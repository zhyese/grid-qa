"""N2 MCP Server：把本系统 6 个能力暴露为 MCP tools，供外部 Agent 调用。

6 能力：
1. search_regulation — 检索运维规程/手册
2. query_equipment_graph — 查设备-故障-处置因果链
3. search_similar_case — 查历史相似故障案例
4. draft_ticket — 生成处置操作票草案（role=admin 权限）
5. graph_query — 知识图谱多跳查询
6. mixed_search — 混合检索（向量+BM25+rerank）

鉴权：简单 token + IP 白名单（PRD Q3 确认）。

用 FastAPI 实现 HTTP 接口（兼容 MCP HTTP 传输协议）。
"""
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.response import success, error
from app.db.session import get_db

router = APIRouter(prefix="/mcp", tags=["MCP 工具总线"])


def _verify_token(authorization: str | None = None) -> bool:
    """验证 MCP token。"""
    if not settings.MCP_TOKEN:
        return True  # 未配置 token = 不鉴权（仅开发环境）
    if not authorization:
        return False
    token = authorization.replace("Bearer ", "").strip()
    return token == settings.MCP_TOKEN


def _check_ip_whitelist(request: Request) -> bool:
    """IP 白名单检查（空=不限）。"""
    whitelist = settings.MCP_IP_WHITELIST.strip()
    if not whitelist:
        return True
    client_ip = request.client.host if request.client else ""
    allowed = [ip.strip() for ip in whitelist.split(",") if ip.strip()]
    return client_ip in allowed


def _auth(request: Request, authorization: str | None = Header(None)):
    """统一鉴权：token + IP 白名单。"""
    if not _check_ip_whitelist(request):
        raise HTTPException(status_code=403, detail="IP 不在白名单")
    if not _verify_token(authorization):
        raise HTTPException(status_code=401, detail="MCP token 无效")


# ===== 6 能力的 MCP tool schema =====

MCP_TOOLS: list[dict] = [
    {
        "name": "search_regulation",
        "description": "检索电网运维规程/手册/标准，获取故障处置的规程依据、限值、标准步骤。",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "检索关键词，如 '主变压器油温高 处置'"}},
            "required": ["query"],
        },
    },
    {
        "name": "query_equipment_graph",
        "description": "查知识图谱中设备的故障-处置因果链（设备→故障→处置 多跳）。",
        "inputSchema": {
            "type": "object",
            "properties": {"entity": {"type": "string", "description": "设备名，如 '1号主变'"}},
            "required": ["entity"],
        },
    },
    {
        "name": "search_similar_case",
        "description": "查历史相似故障案例（故障案例库），看历史上类似故障怎么处理的。",
        "inputSchema": {
            "type": "object",
            "properties": {"symptom": {"type": "string", "description": "故障症状描述"}},
            "required": ["symptom"],
        },
    },
    {
        "name": "draft_ticket",
        "description": "生成处置操作票草案（步骤/安措/风险）。需要 admin 权限。",
        "inputSchema": {
            "type": "object",
            "properties": {"task": {"type": "string", "description": "操作任务，如 '1号主变由运行转检修'"}},
            "required": ["task"],
        },
    },
    {
        "name": "graph_query",
        "description": "知识图谱多跳查询：从指定实体出发，查询 N 跳影响传播链。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "起始实体名"},
                "depth": {"type": "integer", "description": "查询深度（1-5）", "default": 3},
            },
            "required": ["entity"],
        },
    },
    {
        "name": "mixed_search",
        "description": "混合检索（向量+BM25+rerank），返回 Top-K 相关文档分块。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索查询"},
                "topk": {"type": "integer", "description": "返回条数", "default": 5},
            },
            "required": ["query"],
        },
    },
]


@router.get("/tools/list")
async def list_tools():
    """列出所有可用的 MCP tools（无需鉴权，供发现用）。"""
    return success(data={"tools": MCP_TOOLS})


@router.post("/tools/list")
async def list_tools_post(request: Request, authorization: str | None = Header(None)):
    """MCP HTTP 传输协议：POST list_tools（带鉴权）。"""
    _auth(request, authorization)
    return success(data={"tools": MCP_TOOLS})


@router.post("/tools/call")
async def call_tool(
    request: Request,
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """调用 MCP tool。

    Body: {"name": "search_regulation", "arguments": {"query": "主变油温高"}}
    """
    _auth(request, authorization)
    body = await request.json()
    tool_name = body.get("name", "")
    args = body.get("arguments", {}) or {}

    try:
        result = await _dispatch_tool(db, tool_name, args)
        return success(data={"result": result})
    except Exception as e:
        return error(message=f"工具执行失败: {type(e).__name__}: {e}", code=500)


async def _dispatch_tool(db: AsyncSession, name: str, args: dict) -> str:
    """分发到对应 handler（调用 agent_tools 的 Tool.handler / kg_service / retrieval_service）。"""
    if name == "search_regulation":
        from app.services.agent_tools import DEFAULT_REGISTRY
        tool = DEFAULT_REGISTRY.get("search_regulation")
        if tool:
            return await tool.handler(db, None, **args)
        return "工具不可用"

    elif name == "query_equipment_graph":
        from app.services.agent_tools import DEFAULT_REGISTRY
        tool = DEFAULT_REGISTRY.get("query_equipment_graph")
        if tool:
            return await tool.handler(db, None, **args)
        return "工具不可用"

    elif name == "search_similar_case":
        from app.services.agent_tools import DEFAULT_REGISTRY
        tool = DEFAULT_REGISTRY.get("search_similar_case")
        if tool:
            return await tool.handler(db, None, **args)
        return "工具不可用"

    elif name == "draft_ticket":
        from app.services.agent_tools import DEFAULT_REGISTRY
        tool = DEFAULT_REGISTRY.get("draft_ticket")
        if tool:
            return await tool.handler(db, None, **args)
        return "工具不可用"

    elif name == "graph_query":
        from app.services.kg_service import graph_context, get_paths
        entity = args.get("entity", "")
        depth = args.get("depth", 3)
        if not entity:
            return "缺少 entity 参数"
        ctx = await graph_context(entity, 8)
        paths = await get_paths(entity, depth=depth, limit=10)
        lines = ctx + [f"传播链: {' → '.join(p['chain'])}" for p in paths]
        return "\n".join(lines) if lines else "图谱中无该实体相关数据"

    elif name == "mixed_search":
        from app.services.retrieval_service import mixed_search
        query = args.get("query", "")
        topk = args.get("topk", 5)
        if not query:
            return "缺少 query 参数"
        ctx = await mixed_search(db, query, topk)
        lines = []
        for i, c in enumerate(ctx[:topk], 1):
            doc_name = c.get("docName", "")
            chunk = (c.get("chunk") or "")[:200]
            lines.append(f"[{i}] {doc_name}: {chunk}")
        return "\n".join(lines) if lines else "未检索到相关文档"

    else:
        return f"未知工具: {name}"
