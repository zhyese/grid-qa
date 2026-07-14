"""N1 记忆管理 API（列表/删除/统计）。

admin 可查所有用户记忆，普通用户只查自己的（后续可扩展）。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import BizError, success
from app.db.session import get_db
from app.services.agent_memory_service import agent_memory

router = APIRouter(prefix="/memory", tags=["记忆管理"])


@router.get("/list")
async def list_memories(
    userId: str = Query("", description="按用户筛选（空=全部）"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """分页查询记忆列表（含已软删除的，管理员用）。"""
    data = await agent_memory.list_memories(user_id=userId, page=page, size=size)
    return success(data=data)


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str, db: AsyncSession = Depends(get_db)):
    """软删除一条记忆（deleted_at = NOW()，保留 30 天审计后物理删除）。"""
    ok = await agent_memory.forget(memory_id)
    if not ok:
        raise BizError("记忆不存在或已删除", 404)
    return success(message="已删除（软删除，保留30天审计）")


@router.get("/stats")
async def memory_stats(db: AsyncSession = Depends(get_db)):
    """记忆统计：总数/活跃/已删除/用户数/分类分布。"""
    data = await agent_memory.get_stats()
    return success(data=data)
