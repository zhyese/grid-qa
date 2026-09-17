"""遥测历史导入与确定性统计。显式来源、质量过滤，不补造缺失点。"""

import math

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.response import BizError
from app.models.ops_workbench import TelemetryPoint
from app.services.ops_snapshot_service import stamp


async def import_points(db, user, body):
    added, duplicates = 0, 0
    try:
        for point in body.points:
            values = point.model_dump()
            values["observed_at"] = point.observed_at.replace(tzinfo=None)
            old = (
                await db.execute(
                    select(TelemetryPoint).where(
                        TelemetryPoint.tenant_id == user.tenant_id,
                        *[
                            getattr(TelemetryPoint, key) == values[key]
                            for key in ("source", "device_id", "metric", "observed_at")
                        ],
                    )
                )
            ).scalar_one_or_none()
            if old:
                if (old.value, old.unit, old.quality) != (
                    point.value,
                    point.unit,
                    point.quality,
                ):
                    raise BizError(
                        "同来源/设备/指标/时间的点已存在且内容不同；未覆盖原数据", 409
                    )
                duplicates += 1
            else:
                db.add(
                    TelemetryPoint(
                        tenant_id=user.tenant_id, imported_by=user.username, **values
                    )
                )
                await db.flush()
                added += 1
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise BizError("并发导入冲突，请重试（本批次未部分提交）", 409) from exc
    except Exception:
        await db.rollback()
        raise
    return {
        "imported": added,
        "duplicates": duplicates,
        "sourceMode": "user_import",
        "controlExecuted": False,
    }


async def query(db, user, body):
    points = (
        (
            await db.execute(
                select(TelemetryPoint)
                .where(
                    TelemetryPoint.tenant_id == user.tenant_id,
                    TelemetryPoint.device_id == body.device_id,
                    TelemetryPoint.metric == body.metric,
                    TelemetryPoint.source == body.source,
                    TelemetryPoint.observed_at >= body.start.replace(tzinfo=None),
                    TelemetryPoint.observed_at < body.end.replace(tzinfo=None),
                )
                .order_by(TelemetryPoint.observed_at)
                .limit(10001)
            )
        )
        .scalars()
        .all()
    )
    if len(points) > 10000:
        raise BizError("区间超过10000个点，请缩短时间范围", 400)
    good = [p for p in points if p.quality == "good"]
    units = {p.unit for p in good}
    if len(units) > 1:
        raise BizError("区间内存在混合单位，须在数据源完成显式换算后再查询", 400)
    values = [p.value for p in good]
    stats = None
    if values:
        mean = math.fsum(v / len(values) for v in values)
        delta = values[-1] - values[0]
        if not math.isfinite(mean) or not math.isfinite(delta):
            raise BizError("数值超出可安全计算范围，请核对数据源", 400)
        stats = {
            "min": min(values),
            "max": max(values),
            "sampleMean": mean,
            "first": values[0],
            "last": values[-1],
            "delta": values[-1] - values[0],
            "unit": good[0].unit,
            "count": len(values),
        }
    return {
        "deviceId": body.device_id,
        "metric": body.metric,
        "source": body.source,
        "start": body.start.isoformat(),
        "end": body.end.isoformat(),
        "stats": stats,
        "excluded": len(points) - len(good),
        "sourceMode": "user_import",
        "answer": "无有效数据，无法回答。"
        if not stats
        else (
            f"有效样本{len(values)}个，最小{stats['min']:g}，最大{stats['max']:g}，"
            f"样本均值{stats['sampleMean']:g} {stats['unit']}；不据此推断故障原因。"
        ),
        "points": [
            {
                "id": p.id,
                "at": stamp(p.observed_at),
                "value": p.value,
                "unit": p.unit,
                "quality": p.quality,
            }
            for p in points
        ],
        "note": "区间[start,end)，仅good参与统计；均值不是时间加权均值。未插值、未检测采样缺口，不代表实时完整数据。",
    }
