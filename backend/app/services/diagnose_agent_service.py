"""Agentic 诊断：LLM 用 function-calling 自主调用工具做多轮交叉验证诊断。

工具定义成注册表（TOOLS + _HANDLERS），接口中立——Spec 2(MCP) 直接复用包装对外。
循环到 MAX_ITER 或 LLM 给最终答案为止；超限/异常降级到 domain_service.diagnose。
"""
import json
import time

from app.core.obs import degraded
from app.providers.factory import get_llm_provider
from app.services import retrieval_service, kg_service, domain_service

_TOPK = 5


# ---------- 工具实现（包装现有 service，返回 LLM 可读摘要）----------
async def _t_search_regulation(db, model_type, query):
    """检索运维规程/手册。"""
    ctx = await retrieval_service.mixed_search(db, query, _TOPK, model_type=model_type)
    return _fmt_chunks(ctx) or "未检索到相关规程"


async def _t_query_equipment_graph(db, model_type, entity):
    """查设备-故障-处置因果链（Neo4j 图谱）。"""
    rows = await kg_service.graph_context(entity, 8)
    return "\n".join(rows) if rows else "图谱中无该设备相关因果链"


async def _t_search_similar_case(db, model_type, symptom):
    """查历史相似故障案例。"""
    res = await domain_service.similar_case(db, symptom, model_type, _TOPK)
    return _fmt_cases(res.get("cases", [])) or "未找到相似历史案例"


async def _t_draft_ticket(db, model_type, task):
    """生成处置操作票草案。"""
    res = await domain_service.generate_ticket(db, task, model_type, _TOPK)
    return _fmt_ticket(res.get("ticket", {})) or "生成操作票草案失败"


_HANDLERS = {
    "search_regulation": _t_search_regulation,
    "query_equipment_graph": _t_query_equipment_graph,
    "search_similar_case": _t_search_similar_case,
    "draft_ticket": _t_draft_ticket,
}


async def _run_tool(db, model_type, name, args):
    """分发执行；工具失败返回错误串不抛（循环不崩）。"""
    h = _HANDLERS.get(name)
    if not h:
        return f"未知工具: {name}"
    try:
        return await h(db, model_type, **(args or {}))
    except Exception as e:
        degraded(f"agent_tool_{name}", e)
        return f"工具 {name} 执行失败: {type(e).__name__}: {e}"


# ---------- 摘要格式化 ----------
def _fmt_chunks(ctx):
    if not ctx:
        return ""
    return "\n".join(f"[{i}] {(c.get('docName') or '')}: {(c.get('chunk') or '')[:200]}"
                     for i, c in enumerate(ctx[:_TOPK], 1))


def _fmt_cases(cases):
    if not cases:
        return ""
    return "\n".join(f"[{i}] {(c.get('docName') or '')}: {(c.get('text') or '')[:200]}"
                     for i, c in enumerate(cases[:_TOPK], 1))


def _fmt_ticket(ticket):
    if not ticket:
        return ""
    steps = ticket.get("steps") or []
    return (f"设备:{ticket.get('device') or '无'}\n"
            f"步骤:{';'.join(steps[:8]) if steps else '无'}\n"
            f"安措:{';'.join(ticket.get('safety') or []) or '无'}\n"
            f"风险:{';'.join(ticket.get('risks') or []) or '无'}")


# ========== agent 循环 ==========
MAX_ITER = 6

# ---------- OpenAI function-calling 工具 schema（Spec 2 MCP 直接复用）----------
TOOLS = [
    {"type": "function", "function": {
        "name": "search_regulation",
        "description": "检索电网运维规程/手册/标准，获取故障处置的规程依据、限值、标准步骤。",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "检索关键词，如 '主变压器油温高 处置'"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "query_equipment_graph",
        "description": "查知识图谱中设备的故障-处置因果链（设备→故障→处置 多跳）。",
        "parameters": {"type": "object",
                       "properties": {"entity": {"type": "string", "description": "设备名，如 '1号主变'"}},
                       "required": ["entity"]}}},
    {"type": "function", "function": {
        "name": "search_similar_case",
        "description": "查历史相似故障案例（故障案例库），看历史上类似故障怎么处理的。",
        "parameters": {"type": "object",
                       "properties": {"symptom": {"type": "string", "description": "故障症状描述"}},
                       "required": ["symptom"]}}},
    {"type": "function", "function": {
        "name": "draft_ticket",
        "description": "生成处置操作票草案（步骤/安措/风险）。诊断基本明确、需要处置步骤时调用。",
        "parameters": {"type": "object",
                       "properties": {"task": {"type": "string", "description": "操作任务，如 '1号主变由运行转检修'"}},
                       "required": ["task"]}}},
]

_AGENT_SYSTEM = """你是电网运维故障诊断专家。基于故障症状，通过调用工具自主收集证据（规程/图谱/历史案例）进行多轮交叉验证后给出诊断。
规则：
1) 每次可调用 0 个或多个工具；证据充分后停止调用工具，直接输出最终诊断。
2) 最终诊断必须输出严格 JSON：{"causes":[{"name":"可能原因","likelihood":"高/中/低","evidence":"资料依据","handling":"处置措施"}],"summary":"总体判断","risks":["风险点"]}
3) 原因按可能性从高到低排序；只基于工具收集的证据，证据不足如实说明；高风险处置（停电/接地/倒闸）须在 risks 标注。"""


def _to_openai_tool_calls(tool_calls):
    """把内部 dict 形式 tool_calls 转回 openai assistant 消息需要的结构。"""
    return [{"id": tc["id"], "type": "function",
             "function": {"name": tc["name"],
                          "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False)}}
            for tc in tool_calls]


def _inc_metric(iterations):
    try:
        from app.core import metrics
        metrics.DOMAIN_CALLS.labels("diagnose_agent").inc()
        metrics.AGENT_ITERS.observe(iterations)   # Task 4 定义 AGENT_ITERS；此前为 no-op
    except Exception:
        pass


async def diagnose_agent(db, symptom, model_type=None):
    """Agentic 诊断：LLM 自主调工具多轮验证 → {diagnosis, steps[], iterations, degraded, latencyMs}。"""
    t0 = time.perf_counter()
    provider = get_llm_provider(model_type)
    messages = [
        {"role": "system", "content": _AGENT_SYSTEM},
        {"role": "user", "content": f"故障症状：{symptom}"},
    ]
    steps: list[dict] = []
    try:
        resp = None
        for i in range(1, MAX_ITER + 1):
            resp = await provider.chat_with_tools(messages, TOOLS, temperature=0.2, max_tokens=1500)
            if not resp.get("tool_calls"):
                steps.append({"iter": i, "thought": resp.get("content"), "tool": None,
                              "args": None, "result": None})
                break
            # 记 assistant 消息（含 tool_calls，供下一轮引用）
            messages.append({"role": "assistant", "content": resp.get("content") or "",
                             "tool_calls": _to_openai_tool_calls(resp["tool_calls"])})
            for tc in resp["tool_calls"]:
                result = await _run_tool(db, model_type, tc["name"], tc.get("arguments"))
                steps.append({"iter": i, "thought": resp.get("content"), "tool": tc["name"],
                              "args": tc.get("arguments"), "result": (result or "")[:600]})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        else:
            # for-else：break 未触发 → 超 MAX_ITER
            degraded("diagnose_agent_maxiter", RuntimeError(f"max_iter={MAX_ITER}"))
            return await _fallback(db, symptom, model_type, "max_iter", steps, t0)

        diagnosis = domain_service._extract_json(resp.get("content") or "") or \
            {"summary": (resp.get("content") or "")[:500], "causes": []}
    except Exception as e:
        degraded("diagnose_agent_error", e)
        return await _fallback(db, symptom, model_type, f"exception:{type(e).__name__}", steps, t0)

    iters = len(steps)
    _inc_metric(iters)
    return {"symptom": symptom, "diagnosis": diagnosis, "steps": steps, "iterations": iters,
            "degraded": False, "degradeReason": None,
            "latencyMs": int((time.perf_counter() - t0) * 1000)}


async def _fallback(db, symptom, model_type, reason, steps, t0):
    """降级：调现有 single-pass diagnose，保留已收集 steps。"""
    try:
        data = await domain_service.diagnose(db, symptom, model_type)
    except Exception as e:
        degraded("diagnose_agent_fallback", e)
        data = {"diagnosis": {"summary": "诊断生成失败，请参考已收集证据", "causes": []}}
    _inc_metric(len(steps))
    return {"symptom": symptom, "diagnosis": data.get("diagnosis", {"summary": "", "causes": []}),
            "steps": steps, "iterations": len(steps), "degraded": True, "degradeReason": reason,
            "latencyMs": int((time.perf_counter() - t0) * 1000)}
