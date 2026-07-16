"""Agent 工具集：把现有 service 包装成 Tool，返回 LLM 可读摘要。

从 diagnose_agent_service 迁移而来。后续 persona（S2/S3）只需在此新增 Tool 并注册。
"""
from app.services import domain_service, kg_service, retrieval_service
from app.services.agent_runtime import Tool, ToolRegistry

_TOPK = 5


# ---------- 工具实现（包装现有 service，返回 LLM 可读摘要）----------
async def _t_search_regulation(db, model_type, query, tenant=None):
    """检索运维规程/手册。"""
    kwargs = {"model_type": model_type}
    if tenant:
        kwargs["tenant"] = tenant
    ctx = await retrieval_service.mixed_search(db, query, _TOPK, **kwargs)
    return _fmt_chunks(ctx) or "未检索到相关规程"


async def _t_query_equipment_graph(db, model_type, entity, tenant=None):
    """查设备-故障-处置因果链（Neo4j 图谱）。"""
    if tenant:
        rows = await kg_service.graph_context(entity, 8, db=db, tenant=tenant)
    else:
        rows = await kg_service.graph_context(entity, 8)
    return "\n".join(rows) if rows else "图谱中无该设备相关因果链"


async def _t_search_similar_case(db, model_type, symptom, tenant=None):
    """查历史相似故障案例。"""
    kwargs = {"tenant": tenant} if tenant else {}
    res = await domain_service.similar_case(db, symptom, model_type, _TOPK, **kwargs)
    return _fmt_cases(res.get("cases", [])) or "未找到相似历史案例"


async def _t_draft_ticket(db, model_type, task, tenant=None):
    """生成处置操作票草案。"""
    kwargs = {"tenant": tenant} if tenant else {}
    res = await domain_service.generate_ticket(db, task, model_type, _TOPK, **kwargs)
    return _fmt_ticket(res.get("ticket", {})) or "生成操作票草案失败"


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


# ---------- schema ----------
_SCHEMA_QUERY = {"type": "object",
                 "properties": {"query": {"type": "string", "description": "检索关键词，如 '主变压器油温高 处置'"}},
                 "required": ["query"]}
_SCHEMA_ENTITY = {"type": "object",
                  "properties": {"entity": {"type": "string", "description": "设备名，如 '1号主变'"}},
                  "required": ["entity"]}
_SCHEMA_SYMPTOM = {"type": "object",
                   "properties": {"symptom": {"type": "string", "description": "故障症状描述"}},
                   "required": ["symptom"]}
_SCHEMA_TASK = {"type": "object",
                "properties": {"task": {"type": "string", "description": "操作任务，如 '1号主变由运行转检修'"}},
                "required": ["task"]}


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool("search_regulation",
                      "检索电网运维规程/手册/标准，获取故障处置的规程依据、限值、标准步骤。",
                      _SCHEMA_QUERY, _t_search_regulation))
    reg.register(Tool("query_equipment_graph",
                      "查知识图谱中设备的故障-处置因果链（设备→故障→处置 多跳）。",
                      _SCHEMA_ENTITY, _t_query_equipment_graph))
    reg.register(Tool("search_similar_case",
                      "查历史相似故障案例（故障案例库），看历史上类似故障怎么处理的。",
                      _SCHEMA_SYMPTOM, _t_search_similar_case))
    reg.register(Tool("draft_ticket",
                      "生成处置操作票草案（步骤/安措/风险）。诊断基本明确、需要处置步骤时调用。",
                      _SCHEMA_TASK, _t_draft_ticket))
    return reg


DEFAULT_REGISTRY = build_default_registry()


# ===== N2 MCP 工具动态注册 =====

async def register_mcp_tools(registry: ToolRegistry = None) -> int:
    """从 MCP registry 发现外部工具 → schema 转换 → 注册进 ToolRegistry。

    在 main.py lifespan 中 MCP registry 加载后调用。
    Returns: 注册的工具数量
    """
    reg = registry or DEFAULT_REGISTRY
    try:
        from app.mcp.registry import mcp_registry
        from app.mcp.client import mcp_client

        servers = mcp_registry.list_enabled()
        if not servers:
            return 0

        discovered = await mcp_client.discover(servers)
        count = mcp_client.register_tools(reg, discovered)

        # 更新 registry 中的 tools 列表
        for item in discovered:
            mcp_registry.update_tools(item["server"], item.get("tools", []))

        if count:
            print(f"[mcp] 已注册 {count} 个外部 MCP 工具")
        return count
    except Exception as e:
        from app.core.obs import degraded
        degraded("mcp_register_tools", e)
        return 0
