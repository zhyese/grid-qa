"""操作日志服务：写入 + 分页查询（admin 查全部、operator 仅查自己）。"""
from typing import Optional

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.operation_log import OperationLog


async def write_log(db: AsyncSession, operate_user: str, operate_type: str, content: str = "") -> None:
    db.add(OperationLog(operate_user=operate_user, operate_type=operate_type, content=content))
    await db.commit()


async def query_logs(
    db: AsyncSession,
    page: int = 1,
    size: int = 10,
    operate_user: Optional[str] = None,
) -> dict:
    """返回 {total, list[{id,operateUser,operateType,operateTime,content}]}。
    startTime/endTime 时间过滤留至 S8 完善。
    """
    base = select(OperationLog)
    cnt = select(func.count()).select_from(OperationLog)
    if operate_user:
        base = base.where(OperationLog.operate_user == operate_user)
        cnt = cnt.where(OperationLog.operate_user == operate_user)

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
