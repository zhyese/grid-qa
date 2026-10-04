"""遥测历史导入与确定性统计。显式来源、质量过滤，不补造缺失点。

F2 追加：`series()` 供 Agent 工具 `query_telemetry` 使用——按"设备 + 可选指标 + 时间窗"
取确定性统计与趋势方向，并给出可选项提示（设备名对不上时不编造，让 Agent 改问）。
"""

import math
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.response import BizError
from app.models.ops_workbench import TelemetryPoint
from app.services.ops_snapshot_service import stamp

# Agent 侧单次取点上限（低于 API 的 10000：Agent 只做趋势叙事，不需要全量）
AGENT_MAX_POINTS = 2000


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


# ===== F2 时序遥测问答：Agent 工具侧确定性取数 =====

async def _distinct(db, tenant_id: str, column, device_id: str = "", limit: int = 20) -> list[str]:
    stmt = select(column).where(TelemetryPoint.tenant_id == tenant_id)
    if device_id:
        stmt = stmt.where(TelemetryPoint.device_id == device_id)
    rows = (await db.execute(stmt.distinct().order_by(column).limit(limit))).scalars().all()
    return [r for r in rows if r]


def _trend(points: list[TelemetryPoint]) -> dict:
    """确定性趋势：最小二乘斜率 + 最大单步跳变（不做越限判断，限值须查规程）。"""
    if len(points) < 2:
        return {"direction": "样本不足", "slopePerHour": 0.0, "maxJump": 0.0, "jumpAt": ""}
    t0 = points[0].observed_at
    xs = [max(0.0, (p.observed_at - t0).total_seconds() / 3600.0) for p in points]
    ys = [p.value for p in points]
    n = len(xs)
    mx, my = math.fsum(xs) / n, math.fsum(ys) / n
    denom = math.fsum((x - mx) ** 2 for x in xs)
    slope = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom if denom else 0.0
    jump, jump_at = 0.0, ""
    for a, b in zip(points, points[1:]):
        d = abs(b.value - a.value)
        if d > jump:
            jump, jump_at = d, stamp(b.observed_at)
    # 平稳阈值：整段变化小于量程 2% 视为平稳（避免把噪声说成趋势）
    span = max(ys) - min(ys)
    if abs(ys[-1] - ys[0]) <= max(1e-9, span * 0.02) or abs(slope) < 1e-9:
        direction = "平稳"
    else:
        direction = "上升" if slope > 0 else "下降"
    return {"direction": direction, "slopePerHour": round(slope, 4),
            "maxJump": round(jump, 4), "jumpAt": jump_at}


def _downsample(points: list[TelemetryPoint], target: int = 200) -> list[TelemetryPoint]:
    if len(points) <= target:
        return points
    step = len(points) / target
    picked = [points[int(i * step)] for i in range(target)]
    if points[-1] is not picked[-1]:
        picked.append(points[-1])
    return picked


async def series(db, tenant_id: str, device_id: str, metric: str = "",
                 hours: float = 24.0, source: str = "") -> dict:
    """按 设备(+可选指标) + 时间窗 取确定性统计与趋势，供 Agent 工具消费。

    设备/指标对不上时**不编造**：返回 availableDevices / availableMetrics 让 Agent 改问。
    仅统计 quality=good 的点，未插值、未补造缺失点。
    """
    device_id = (device_id or "").strip()
    metric = (metric or "").strip()
    hours = max(0.1, min(float(hours or 24.0), 24 * 90))
    end = datetime.now()
    start = end - timedelta(hours=hours)
    out = {
        "deviceId": device_id, "metric": metric, "source": source,
        "start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"),
        "stats": None, "trend": None, "points": [], "excluded": 0,
        "availableDevices": [], "availableMetrics": [], "note": "",
    }
    if not device_id:
        out["availableDevices"] = await _distinct(db, tenant_id, TelemetryPoint.device_id)
        out["note"] = "未提供设备名，请先用 availableDevices 中的设备名重新调用。"
        return out

    # 1) 设备解析：精确优先，其次模糊（模糊命中多个→让 Agent 消歧）
    exact = (await db.execute(
        select(TelemetryPoint.id).where(
            TelemetryPoint.tenant_id == tenant_id,
            TelemetryPoint.device_id == device_id,
        ).limit(1)
    )).scalar_one_or_none()
    resolved = device_id
    if exact is None:
        cands = (await db.execute(
            select(TelemetryPoint.device_id).where(
                TelemetryPoint.tenant_id == tenant_id,
                TelemetryPoint.device_id.like(f"%{device_id}%"),
            ).distinct().limit(20)
        )).scalars().all()
        cands = [c for c in cands if c]
        if not cands:
            out["availableDevices"] = await _distinct(db, tenant_id, TelemetryPoint.device_id)
            out["note"] = (f"遥测库中不存在设备「{device_id}」，且无模糊匹配。"
                           "请改用 availableDevices 中的设备名；若列表为空说明该租户尚未导入遥测。")
            return out
        if len(cands) > 1:
            out["availableDevices"] = cands
            out["note"] = f"设备名「{device_id}」匹配到多个设备，请指定其一后重新调用。"
            return out
        resolved = cands[0]
    out["deviceId"] = resolved

    # 2) 指标解析（未给指标 → 汇总该设备全部指标）
    metrics = await _distinct(db, tenant_id, TelemetryPoint.metric, device_id=resolved)
    if metric:
        hit = [m for m in metrics if m == metric]
        if not hit:
            hit = [m for m in metrics if metric in m or m in metric]
        if not hit:
            out["availableMetrics"] = metrics
            out["note"] = (f"设备「{resolved}」没有指标「{metric}」，"
                           "请改用 availableMetrics 中的指标名后重新调用。")
            return out
        out["metric"] = hit[0]

    # 3) 取数（仅 good 参与统计）
    stmt = select(TelemetryPoint).where(
        TelemetryPoint.tenant_id == tenant_id,
        TelemetryPoint.device_id == resolved,
        TelemetryPoint.observed_at >= start,
        TelemetryPoint.observed_at < end,
    )
    if out["metric"]:
        stmt = stmt.where(TelemetryPoint.metric == out["metric"])
    if source:
        stmt = stmt.where(TelemetryPoint.source == source)
    rows = (await db.execute(
        stmt.order_by(TelemetryPoint.observed_at).limit(AGENT_MAX_POINTS + 1)
    )).scalars().all()
    if not rows:
        out["availableMetrics"] = metrics
        out["note"] = (f"设备「{resolved}」在近 {hours:g} 小时内无遥测数据"
                       "（可能未导入或时间窗内确实无采样）。请如实说明无数据，不得推测数值。")
        return out

    good = [p for p in rows if p.quality == "good"]
    out["excluded"] = len(rows) - len(good)
    if not good:
        out["note"] = "时间窗内全部样本质量非 good，无法给出可信统计；请如实说明。"
        return out
    units = {p.unit for p in good}
    values = [p.value for p in good]
    mean = math.fsum(values) / len(values)
    if len(units) > 1:
        out["note"] = f"区间内存在混合单位 {sorted(units)}，无法合并统计；请缩小时间窗或指定指标。"
        out["availableMetrics"] = metrics
        return out
    out["stats"] = {
        "min": min(values), "max": max(values), "mean": round(mean, 4),
        "first": values[0], "last": values[-1], "delta": round(values[-1] - values[0], 4),
        "unit": good[0].unit, "count": len(values),
    }
    out["trend"] = _trend(good)
    # 前端迷你趋势图的降采样目标点数（0=不返回序列，只给统计）
    from app.config import settings as _s
    _chart_max = int(getattr(_s, "TELEMETRY_CHART_MAX_POINTS", 200))
    if _chart_max > 0:
        out["points"] = [
            {"at": stamp(p.observed_at), "value": p.value, "unit": p.unit}
            for p in _downsample(good, target=_chart_max)
        ]
    out["note"] = ("仅 quality=good 样本参与统计；未插值、未补造缺失点；"
                   "趋势为线性斜率方向，越限判断须结合规程限值，本工具不下越限结论。")
    return out
