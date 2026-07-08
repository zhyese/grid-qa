"""通用 Agent 引擎：Tool/ToolRegistry/Persona/run_agent/AgentResult。

把诊断专用 agent 抽象成 persona 驱动的通用 ReAct 引擎。
循环 / per-tool 异常隔离 / 降级 / 指标沿用 diagnose_agent_service 既有实现。
工具集中定义在 agent_tools.py（run_agent 内 lazy import 避免循环）。
"""
import json
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.obs import degraded
from app.providers.factory import get_llm_provider

MAX_ITER_DEFAULT = 6

# S4：高风险工具按 role 限制（未列出的全员可调）。仅 ctx 提供时生效（ctx=None 零回归）。
tool_permissions: dict[str, list[str]] = {
    "draft_ticket": ["admin"],
}


@dataclass
class Tool:
    """一个工具 = OpenAI schema + handler，绑成单一对象避免 schema/handler 不一致。"""
    name: str
    description: str
    parameters: dict
    handler: Callable[[AsyncSession, Optional[str], dict], Awaitable[str]]

    @property
    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}


class ToolRegistry:
    """工具注册表：register/get/schemas_for/run。run 内含 per-tool 异常隔离。"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def schemas_for(self, names: list[str]) -> list[dict]:
        out = []
        for n in names:
            t = self._tools.get(n)
            if t:
                out.append(t.schema)
        return out

    async def run(self, db, model_type, name: str, args: dict,
                  ctx: dict | None = None) -> tuple[str, bool]:
        """执行工具，返回 (result_text, error)。权限检查(S4) + 审计(S4) + 失败不抛。

        ctx: {username, tenant, role, persona, iter} 可选。
        ctx=None 时跳过权限与审计（diagnose 等老链路零回归）。
        """
        # 权限检查（仅 ctx 提供时；高风险工具按 role 限）
        allowed_roles = tool_permissions.get(name)
        if ctx is not None and allowed_roles:
            role = ctx.get("role", "")
            if role not in allowed_roles:
                try:
                    from app.core import metrics
                    metrics.AGENT_TOOL_DENIED.labels(name).inc()
                except Exception:
                    pass
                return f"权限不足：工具 {name} 需 {allowed_roles} 角色", True
        t = self._tools.get(name)
        if not t:
            return f"未知工具: {name}", True
        error = False
        try:
            result = await t.handler(db, model_type, **(args or {}))
        except Exception as e:
            degraded(f"agent_tool_{name}", e)
            result = f"工具 {name} 执行失败: {type(e).__name__}: {e}"
            error = True
        # 审计（fire-and-forget bg task，仅 ctx 提供时；仿 rewrite_event 独立 session）
        if ctx is not None:
            try:
                import asyncio
                from app.services.agent_tool_audit_service import log_tool_call
                asyncio.ensure_future(log_tool_call(
                    persona=ctx.get("persona", ""), tool=name,
                    iter=ctx.get("iter", 0), args=args or {}, result=result or "",
                    error=error, username=ctx.get("username", ""),
                    tenant=ctx.get("tenant", "default"), role=ctx.get("role", ""),
                    degraded_flag=error,
                ))
            except Exception:
                pass
        return result, error


@dataclass
class Persona:
    """场景配置：system prompt + 工具子集 + 参数 + 输出格式 + 降级目标。"""
    name: str
    system_prompt: str
    allowed_tools: list[str]
    max_iter: int = MAX_ITER_DEFAULT
    temperature: float = 0.2
    max_tokens: int = 1500
    output_format: str = "text"          # "json" | "text"
    fallback: Optional[Callable[[AsyncSession, str, Optional[str]], Awaitable[dict]]] = None
    config_source: str = "code"          # 预留 S5："code" | "db"


@dataclass
class AgentResult:
    answer: object                        # str（text）| dict（json）
    steps: list[dict]                     # [{iter, thought, tool, args, result, error}]
    iterations: int
    degraded: bool
    degrade_reason: Optional[str]
    latency_ms: int
    persona: str
    tools_used: list[str]


def _extract_json(ans: str) -> Optional[dict | list]:
    """从 LLM 输出中正则提取 JSON（自包含副本，与 domain_service 一致，不引入跨服务依赖）。"""
    m = re.search(r"(\{.*\}|\[.*\])", ans or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _to_openai_tool_calls(tool_calls):
    """内部 dict 形式 tool_calls → openai assistant 消息需要的结构。"""
    return [{"id": tc["id"], "type": "function",
             "function": {"name": tc["name"],
                          "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False)}}
            for tc in tool_calls]


def _inc_metrics(persona: str, iterations: int) -> None:
    try:
        from app.core import metrics
        metrics.AGENT_CALLS.labels(persona).inc()
        metrics.AGENT_ITERS.observe(iterations)
    except Exception:
        pass


async def run_agent(db: AsyncSession, persona: Persona, user_msg: str,
                    model_type: Optional[str] = None,
                    registry: Optional[ToolRegistry] = None,
                    ctx: Optional[dict] = None,
                    on_step: Optional[Callable[[dict], None]] = None) -> AgentResult:
    """通用 ReAct 引擎：LLM 自主调工具多轮验证，persona 驱动全流程。

    ctx: {username, tenant, role} 可选，透传给 ToolRegistry.run 做审计+权限（S4）。
    ctx=None 时无审计/权限（diagnose 老链路零回归）。
    on_step: 每完成一步（工具步/收尾步）后回调 step dict（S2 流式思考链用）。
    on_step=None 时无回调（默认，零回归）。
    """
    if registry is None:
        from app.services.agent_tools import DEFAULT_REGISTRY
        registry = DEFAULT_REGISTRY
    t0 = time.perf_counter()
    provider = get_llm_provider(model_type)
    messages = [
        {"role": "system", "content": persona.system_prompt},
        {"role": "user", "content": user_msg},
    ]
    steps: list[dict] = []
    resp = None
    answer = None
    try:
        for i in range(1, persona.max_iter + 1):
            resp = await provider.chat_with_tools(
                messages, registry.schemas_for(persona.allowed_tools),
                temperature=persona.temperature, max_tokens=persona.max_tokens)
            if not resp.get("tool_calls"):
                steps.append({"iter": i, "thought": resp.get("content"), "tool": None,
                              "args": None, "result": None, "error": False})
                if on_step:
                    on_step(steps[-1])
                break
            messages.append({"role": "assistant", "content": resp.get("content") or "",
                             "tool_calls": _to_openai_tool_calls(resp["tool_calls"])})
            for tc in resp["tool_calls"]:
                step_ctx = {**(ctx or {}), "persona": persona.name, "iter": i}
                result, err = await registry.run(db, model_type, tc["name"], tc.get("arguments"), ctx=step_ctx)
                steps.append({"iter": i, "thought": resp.get("content"), "tool": tc["name"],
                              "args": tc.get("arguments"), "result": (result or "")[:600], "error": err})
                if on_step:
                    on_step(steps[-1])
                try:
                    from app.core import metrics
                    metrics.AGENT_TOOL_CALLS.labels(persona.name, tc["name"]).inc()
                except Exception:
                    pass
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        else:
            # for-else：break 未触发 → 超 max_iter
            degraded("agent_max_iter", RuntimeError(f"persona={persona.name} max_iter={persona.max_iter}"))
            return await _fallback(db, persona, user_msg, model_type, steps, t0, "max_iter")

        if persona.output_format == "json":
            answer = _extract_json(resp.get("content") or "") or \
                {"summary": (resp.get("content") or "")[:500]}
        else:
            answer = resp.get("content") or ""
    except Exception as e:
        degraded("agent_error", e)
        return await _fallback(db, persona, user_msg, model_type, steps, t0, f"exception:{type(e).__name__}")

    iters = len(steps)
    _inc_metrics(persona.name, iters)
    return AgentResult(
        answer=answer, steps=steps, iterations=iters,
        degraded=False, degrade_reason=None,
        latency_ms=int((time.perf_counter() - t0) * 1000),
        persona=persona.name,
        tools_used=sorted({s["tool"] for s in steps if s["tool"]}),
    )


async def _fallback(db, persona: Persona, user_msg: str, model_type,
                    steps: list[dict], t0: float, reason: str) -> AgentResult:
    """降级：调 persona.fallback；失败再退到最小化结果。保留已收集 steps。"""
    answer = None
    if persona.fallback:
        try:
            answer = await persona.fallback(db, user_msg, model_type)
        except Exception as e:
            degraded("agent_fallback", e)
    _inc_metrics(persona.name, len(steps))
    return AgentResult(
        answer=answer or {"summary": "agent 降级，未能生成结果", "degradeReason": reason},
        steps=steps, iterations=len(steps),
        degraded=True, degrade_reason=reason,
        latency_ms=int((time.perf_counter() - t0) * 1000),
        persona=persona.name,
        tools_used=sorted({s["tool"] for s in steps if s["tool"]}),
    )
