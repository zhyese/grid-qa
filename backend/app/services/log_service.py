"""操作日志服务：写入 + 分页查询（角色过滤 + 时间过滤）。"""
from datetime import datetime
from typing import Optional

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.operation_log import OperationLog


def _parse_time(s: Optional[str]) -> Optional[datetime]:
    """兼容 'YYYY-MM-DD HH:MM:SS' / ISO / 日期 / 时间戳。"""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromtimestamp(float(s))
    except (ValueError, TypeError):
        return None


async def write_log(db: AsyncSession, operate_user: str, operate_type: str, content: str = "") -> None:
    db.add(OperationLog(operate_user=operate_user, operate_type=operate_type, content=content))
    await db.commit()


async def query_logs(
    db: AsyncSession,
    page: int = 1,
    size: int = 10,
    operate_user: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> dict:
    base = select(OperationLog)
    cnt = select(func.count()).select_from(OperationLog)
    if operate_user:
        base = base.where(OperationLog.operate_user == operate_user)
        cnt = cnt.where(OperationLog.operate_user == operate_user)
    st, et = _parse_time(start_time), _parse_time(end_time)
    if st:
        base = base.where(OperationLog.operate_time >= st)
        cnt = cnt.where(OperationLog.operate_time >= st)
    if et:
        base = base.where(OperationLog.operate_time <= et)
        cnt = cnt.where(OperationLog.operate_time <= et)

    total = (await db.execute(cnt)).scalar() or 0
    rows = (
        await db.execute(
            base.order_by(desc(OperationLog.operate_time)).offset((page - 1) * size).limit(size)
        )
    ).scalars().all()

    return {
        "total": total,
        "list": [
            {
                "id": r.id,
                "operateUser": r.operate_user,
                "operateType": r.operate_type,
                "operateTime": r.operate_time.strftime("%Y-%m-%d %H:%M:%S") if r.operate_time else "",
                "content": r.content,
            }
            for r in rows
        ],
    }
