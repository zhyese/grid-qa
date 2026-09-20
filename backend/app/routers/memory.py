"""N1 记忆管理 API（列表/删除/统计）。

治理：全部端点需登录；操作以认证租户为域（跨租户记忆不可见/不可删）。
admin 可查本租户全部用户记忆，普通用户只查自己的（后续可扩展）。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import BizError, success
from app.db.session import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.services.agent_memory_service import agent_memory

router = APIRouter(prefix="/memory", tags=["记忆管理"])


@router.get("/list")
async def list_memories(
    userId: str = Query("", description="按用户筛选（空=全部）"),
    agentId: str = Query("", description="按 persona 筛选（空=全部）"),
    scope: str = Query("", description="按归属域筛选 user|device（空=全部）"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """分页查询记忆列表（含已软删除的，租户域内）。"""
    data = await agent_memory.list_memories(
        user_id=userId, page=page, size=size,
        tenant_id=getattr(user, "tenant_id", None) or "default",
        agent_id=agentId, scope=scope,
    )
    return success(data=data)


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """软删除一条记忆（deleted_at = NOW()，保留 30 天审计后物理删除；租户域内）。"""
    ok = await agent_memory.forget(
        memory_id, tenant_id=getattr(user, "tenant_id", None) or "default",
    )
    if not ok:
        raise BizError("记忆不存在或已删除", 404)
    return success(message="已删除（软删除，保留30天审计）")


@router.get("/stats")
async def memory_stats(
    agentId: str = Query("", description="按 persona 筛选（空=全部）"),
    scope: str = Query("", description="按归属域筛选 user|device（空=全部）"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """记忆统计：总数/活跃/已删除/用户数/分类分布（租户域内）。"""
    data = await agent_memory.get_stats(
        tenant_id=getattr(user, "tenant_id", None) or "default",
        agent_id=agentId, scope=scope,
    )
    return success(data=data)


@router.post("/recall")
async def recall_memory(
    body: dict,
    user: User = Depends(get_current_user),
):
    """召回当前用户与查询相关的记忆（显式沉淀域；外部服务不可用降级返回空）。"""
    query = (body or {}).get("query", "")
    if not query.strip():
        raise BizError("query 不能为空", 400)
    text = await agent_memory.recall(
        query.strip(), user.username,
        scope=(body or {}).get("scope", "user"),
        tenant_id=getattr(user, "tenant_id", None) or "default",
        agent_id=(body or {}).get("agentId", ""),
    )
    return success(data={"query": query.strip(), "memory": text or ""})
