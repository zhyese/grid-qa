"""操作日志自动归档（BRD §4.5.2）。

超过保留期(LOG_ARCHIVE_DAYS, 默认 90 天)的 operation_logs：
1. 导出到 data/log_archive/logs_YYYYMMDD_HHMMSS.jsonl（便于离线审计/检索）
2. 从 DB 删除（释放空间）

后台每日跑一次 archive_loop（main.py lifespan 注册），管理员也可手动触发。
归档文件落在 backend 持久卷 /app/data 下，容器重建不丢。
"""
import asyncio
import datetime
import json
import os

from sqlalchemy import delete, func, select

from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.operation_log import OperationLog

ARCHIVE_DIR = "data/log_archive"


def _retention_days() -> int:
    """保留天数：优先 settings.LOG_ARCHIVE_DAYS，否则环境变量，默认 90。"""
    try:
        from app.config import settings
        v = getattr(settings, "LOG_ARCHIVE_DAYS", None)
        if v:
            return int(v)
    except Exception:
        pass
    return int(os.environ.get("LOG_ARCHIVE_DAYS", "90"))


def _enabled() -> bool:
    try:
        from app.config import settings
        if getattr(settings, "LOG_ARCHIVE_ENABLE", None) is not None:
            return bool(settings.LOG_ARCHIVE_ENABLE)
    except Exception:
        pass
    return os.environ.get("LOG_ARCHIVE_ENABLE", "true").lower() != "false"


async def archive_stats() -> dict:
    """归档统计：总数、最早/最晚时间、各类型计数、超期待归档数。"""
    async with AsyncSessionLocal() as db:
        total = (await db.execute(select(func.count()).select_from(OperationLog))).scalar() or 0
        oldest = (await db.execute(select(func.min(OperationLog.operate_time)))).scalar()
        newest = (await db.execute(select(func.max(OperationLog.operate_time)))).scalar()
        by_type = {r[0]: r[1] for r in (await db.execute(
            select(OperationLog.operate_type, func.count()).group_by(OperationLog.operate_type)
        )).all()}
        cutoff = datetime.datetime.now() - datetime.timedelta(days=_retention_days())
        pending = (await db.execute(
            select(func.count()).select_from(OperationLog).where(OperationLog.operate_time < cutoff)
        )).scalar() or 0
    return {
        "total": total,
        "oldest": oldest.strftime("%Y-%m-%d %H:%M:%S") if oldest else None,
        "newest": newest.strftime("%Y-%m-%d %H:%M:%S") if newest else None,
        "byType": by_type,
        "retentionDays": _retention_days(),
        "pendingArchive": pending,  # 已超期待归档条数
        "enabled": _enabled(),
    }


async def archive_old_logs(retention_days: int | None = None) -> dict:
    """归档超期日志→jsonl 文件→删除。返回归档条数/文件名。"""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    days = retention_days or _retention_days()
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(OperationLog).where(OperationLog.operate_time < cutoff).order_by(OperationLog.operate_time)
        )).scalars().all()
        if not rows:
            return {"archived": 0, "file": None, "retentionDays": days}
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"logs_{ts}.jsonl"
        path = os.path.join(ARCHIVE_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({
                    "id": r.id, "user": r.operate_user, "type": r.operate_type,
                    "content": r.content,
                    "time": r.operate_time.strftime("%Y-%m-%d %H:%M:%S") if r.operate_time else None,
                }, ensure_ascii=False) + "\n")
        # 按主键批量删
        ids = [r.id for r in rows]
        await db.execute(delete(OperationLog).where(OperationLog.id.in_(ids)))
        await db.commit()
    return {"archived": len(rows), "file": filename, "retentionDays": days}


async def archive_loop():
    """后台周期归档（每日）。失败降级不中断。"""
    while True:
        try:
            if _enabled():
                res = await archive_old_logs()
                if res["archived"]:
                    import logging
                    logging.getLogger("app").info(f"[log-archive] 归档 {res['archived']} 条 → {res['file']}")
        except Exception as e:
            degraded("log_archive", e)
        await asyncio.sleep(86400)  # 24h
