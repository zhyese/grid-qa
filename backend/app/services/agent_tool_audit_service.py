"""Agent 工具调用审计：bg task 独立 session 记录 + 分页查询。

仿 rewrite_event_service：bg task 用独立 AsyncSessionLocal（避免 session 并发 500 教训）。
由 ToolRegistry.run 在每次工具调用后 fire-and-forget 触发。
"""
import json

from sqlalchemy import desc, func, select

from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.agent_tool_call import AgentToolCall


async def log_tool_call(persona: str, tool: str, iter: int, args: dict, result: str,
                        error: bool, username: str, tenant: str, role: str,
                        degraded_flag: bool = False) -> None:
    """记录一条工具调用审计。fire-and-forget bg task 调；失败不抛（degraded 上报）。"""
    try:
        async with AsyncSessionLocal() as db:
            db.add(AgentToolCall(
                persona=persona, tool=tool, iter=iter,
                args_json=json.dumps(args or {}, ensure_ascii=False)[:500],
                result_summary=(result or "")[:500],
                error=bool(error), username=username or "",
                tenant=tenant or "default", role=role or "",
                degraded=bool(degraded_flag),
            ))
            await db.commit()
    except Exception as e:
        degraded("agent_tool_audit_log", e)


async def query_tool_calls(page: int = 1, size: int = 20,
                           persona: str | None = None, tool: str | None = None,
                           username: str | None = None) -> dict:
    """分页查询工具调用审计（admin 用）。"""
    try:
        async with AsyncSessionLocal() as db:
            base = select(AgentToolCall)
            cnt = select(func.count()).select_from(AgentToolCall)
            if persona:
                base = base.where(AgentToolCall.persona == persona)
                cnt = cnt.where(AgentToolCall.persona == persona)
            if tool:
                base = base.where(AgentToolCall.tool == tool)
                cnt = cnt.where(AgentToolCall.tool == tool)
            if username:
                base = base.where(AgentToolCall.username == username)
                cnt = cnt.where(AgentToolCall.username == username)
            total = (await db.execute(cnt)).scalar() or 0
            rows = (await db.execute(
                base.order_by(desc(AgentToolCall.ts)).offset((page - 1) * size).limit(size)
            )).scalars().all()
            return {"total": total, "list": [{
                "id": r.id,
                "ts": r.ts.strftime("%Y-%m-%d %H:%M:%S") if r.ts else "",
                "persona": r.persona, "tool": r.tool, "iter": r.iter,
                "args": r.args_json, "result": r.result_summary,
                "error": bool(r.error), "username": r.username,
                "tenant": r.tenant, "role": r.role, "degraded": bool(r.degraded),
            } for r in rows]}
    except Exception as e:
        degraded("agent_tool_audit_query", e)
        return {"total": 0, "list": []}
