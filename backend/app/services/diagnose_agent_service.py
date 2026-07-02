"""Agentic 诊断：LLM 用 function-calling 自主调用工具做多轮交叉验证诊断。

工具定义成注册表（TOOLS + _HANDLERS），接口中立——Spec 2(MCP) 直接复用包装对外。
循环到 MAX_ITER 或 LLM 给最终答案为止；超限/异常降级到 domain_service.diagnose。
"""
from app.core.obs import degraded
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
