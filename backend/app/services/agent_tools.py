"""Agent 工具集：把现有 service 包装成 Tool，返回 LLM 可读摘要。

从 diagnose_agent_service 迁移而来。后续 persona（S2/S3）只需在此新增 Tool 并注册。
"""
from app.config import settings
from app.services import (
    domain_service,
    kg_service,
    retrieval_service,
    telemetry_history_service,
    ticket_lifecycle_service,
)
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


async def _t_create_ticket(db, model_type, task, device="", ticketType="操作票",
                           steps=None, safety=None, risks=None,
                           sourceRef="", tenant=None, creator=""):
    """把处置方案落库成两票草稿（source_ref 幂等，重复创建返回既有票）。"""
    task = (task or "").strip()
    if not task:
        return "创建失败：task 不能为空"
    kwargs = {"tenant": tenant} if tenant else {}
    t = await ticket_lifecycle_service.create_ticket(
        db, ticket_type=ticketType or "操作票", task=task, device=device or "",
        steps=steps or [], safety=safety or [], risks=risks or [],
        source_ref=sourceRef or None, creator=creator, **kwargs)
    return (f"已创建工单 {t['id']}（草稿，状态:{t['status']}），标题:{t['title']}。"
            f"可调用 submit_ticket 提交审核")


async def _t_submit_ticket(db, model_type, ticketId, tenant=None):
    """提交工单进审核（自动跑审核引擎，高分自动初审通过）。"""
    kwargs = {"tenant": tenant} if tenant else {}
    try:
        t = await ticket_lifecycle_service.submit_for_review(db, ticketId, **kwargs)
    except ValueError as e:
        return f"提交失败：{e}"
    return (f"工单 {t['id']} 已提交审核，状态:{t['status']}，"
            f"审核得分:{t.get('reviewScore', 0)}")


async def _t_query_telemetry(db, model_type, deviceId, metric="", hours=24,
                             source="", tenant=None):
    """F2 时序遥测问答：查设备遥测数值序列 + 确定性统计/趋势（只读，不补造数据）。"""
    res = await telemetry_history_service.series(
        db, tenant or "default", deviceId, metric=metric,
        hours=hours, source=source,
    )
    return _fmt_telemetry(res)


async def _t_assess_evidence(db, model_type, query, topk=8, tenant=None):
    """H6 单代理检索循环的"grade"步：用 CRAG 口径评估当前证据强度并给出停止/续检建议。

    复用现成 CRAG（rag/crag.py）：evidence_strength + grade，不额外调评估 LLM，
    因此延迟与成本都低，可以放心在循环里每轮调用。
    """
    from app.rag import crag

    kwargs = {"tenant": tenant} if tenant else {}
    topk = max(1, min(int(topk or 8), 20))
    try:
        ctxs = await retrieval_service.mixed_search(
            db, query, topk, model_type=model_type, **kwargs)
    except Exception as e:
        from app.core.obs import degraded
        degraded("assess_evidence_retrieval", e)
        ctxs = []
    rerank_ok = bool(settings.RERANK_ENABLE)
    top1 = float(ctxs[0].get("score", 0.0) or 0.0) if ctxs else 0.0
    es = crag.evidence_strength(top1=top1, rerank_ok=rerank_ok)
    grade, reason = crag.grade(top1, len(ctxs), rerank_ok, es=es)
    return _fmt_assessment(query, ctxs, grade, reason, es, rerank_ok)


def _fmt_assessment(query, ctxs, grade, reason, es, rerank_ok) -> str:
    """把 CRAG 分级渲染成带"停止准则建议"的文本，供 Agent 决定是否继续检索。"""
    from app.rag import crag as _crag
    advice = {
        _crag.GRADE_CORRECT: "证据充分 → 可以停止检索，直接给出结论。",
        _crag.GRADE_AMBIGUOUS: "证据有限 → 可再换 1~2 组关键词或补充设备名后重试；"
                               "若下一轮仍是本档，请直接作答并明确标注不确定部分。",
        _crag.GRADE_INCORRECT: "证据不足 → 请改写查询或换检索角度再试；"
                               "若连续 2 轮仍为本档，必须停止并如实说明『现有资料无法确认』。",
    }.get(grade, "按上述证据情况决定是否继续检索。")
    lines = [
        f"评估对象:{query}",
        f"分级:{grade}（{reason}）",
        f"证据强度:{'评估器降级' if es is None else f'{es:.2f}/1.0'}"
        f"｜rerank:{'启用' if rerank_ok else '未启用（分数不可靠，按保守口径分级）'}"
        f"｜命中:{len(ctxs)} 条",
        f"停止准则建议:{advice}",
    ]
    if ctxs:
        lines.append("当前命中来源:")
        lines.extend(
            f"  [{i}] {(c.get('docName') or '')}: {(c.get('chunk') or '')[:120]}"
            for i, c in enumerate(ctxs[:5], 1)
        )
    else:
        lines.append("当前命中来源:无")
    return "\n".join(lines)


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


def _fmt_telemetry(res: dict) -> str:
    """F2：把确定性遥测统计渲染成 LLM 可读文本（无数据时如实拒答 + 给出可选项）。"""
    if not res.get("stats"):
        lines = [f"设备:{res.get('deviceId') or '（未指定）'} 指标:{res.get('metric') or '（未指定）'}",
                 f"区间:{res.get('start')} ~ {res.get('end')}"]
        if res.get("availableDevices"):
            lines.append("可选设备:" + ", ".join(res["availableDevices"][:15]))
        if res.get("availableMetrics"):
            lines.append("可选指标:" + ", ".join(res["availableMetrics"][:15]))
        lines.append("结论:" + (res.get("note") or "无有效遥测数据，无法回答。"))
        return "\n".join(lines)

    s, t = res["stats"], res.get("trend") or {}
    lines = [
        f"设备:{res['deviceId']} 指标:{res['metric']} 区间:{res['start']} ~ {res['end']}",
        f"有效样本:{s['count']} 个（排除非good {res.get('excluded', 0)} 个），单位:{s['unit']}",
        (f"最小 {s['min']:g} / 最大 {s['max']:g} / 均值 {s['mean']:g} / "
         f"首值 {s['first']:g} / 末值 {s['last']:g}（区间变化 {s['delta']:+g}）"),
        (f"趋势:{t.get('direction', '-')}（斜率 {t.get('slopePerHour', 0):+g} {s['unit']}/h），"
         f"最大单步跳变 {t.get('maxJump', 0):g} 于 {t.get('jumpAt') or '-'}"),
    ]
    pts = res.get("points") or []
    if pts:
        shown = ", ".join(f"{p['value']:g}@{p['at'][5:]}" for p in pts[:40])
        lines.append(f"序列(降采样 {len(pts)} 点，前 40): {shown}")
    lines.append("提示:" + (res.get("note") or ""))
    return "\n".join(lines)


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
_SCHEMA_CREATE_TICKET = {"type": "object",
                         "properties": {
                             "task": {"type": "string", "description": "操作任务，如 '1号主变由运行转检修'"},
                             "device": {"type": "string", "description": "设备名"},
                             "ticketType": {"type": "string", "enum": ["操作票", "工作票"], "description": "票据类型，默认操作票"},
                             "steps": {"type": "array", "items": {"type": "string"}, "description": "操作步骤列表"},
                             "safety": {"type": "array", "items": {"type": "string"}, "description": "安全措施列表"},
                             "risks": {"type": "array", "items": {"type": "string"}, "description": "风险/危险点列表"},
                             "sourceRef": {"type": "string", "description": "来源关联（如 qa:会话id），幂等键"}},
                         "required": ["task"]}
_SCHEMA_SUBMIT_TICKET = {"type": "object",
                         "properties": {"ticketId": {"type": "string", "description": "工单 id"}},
                         "required": ["ticketId"]}
_SCHEMA_TELEMETRY = {"type": "object",
                     "properties": {
                         "deviceId": {"type": "string", "description": "设备名，如 '1号主变'（可模糊匹配）"},
                         "metric": {"type": "string", "description": "指标名，如 '油温'/'负荷'；留空则汇总该设备全部指标"},
                         "hours": {"type": "number", "description": "回看小时数，默认 24，最大 2160（90 天）"},
                         "source": {"type": "string", "description": "数据来源（可选），留空=全部来源"},
                     },
                     "required": ["deviceId"]}
_SCHEMA_ASSESS = {"type": "object",
                  "properties": {
                      "query": {"type": "string", "description": "本轮用于检索的查询（可用改写后的问法）"},
                      "topk": {"type": "integer", "description": "评估取前 K 条，默认 8，最大 20"},
                  },
                  "required": ["query"]}


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool("search_regulation",
                      "检索电网运维规程/手册/标准，获取故障处置的规程依据、限值、标准步骤。",
                      _SCHEMA_QUERY, _t_search_regulation, required_roles=[]))
    reg.register(Tool("query_equipment_graph",
                      "查知识图谱中设备的故障-处置因果链（设备→故障→处置 多跳）。",
                      _SCHEMA_ENTITY, _t_query_equipment_graph, required_roles=[]))
    reg.register(Tool("search_similar_case",
                      "查历史相似故障案例（故障案例库），看历史上类似故障怎么处理的。",
                      _SCHEMA_SYMPTOM, _t_search_similar_case, required_roles=[]))
    # F2 时序遥测问答：只读工具，回答"某设备某指标最近 N 小时的数值/趋势"类问题。
    # 开关：TELEMETRY_QA_ENABLE（问答侧）或 PROACTIVE_TELEMETRY_ENABLE（主动运维侧），
    # 任一开启即注册；关=现状（工具不可见，LLM 不会调用）。
    if (getattr(settings, "TELEMETRY_QA_ENABLE", False)
            or getattr(settings, "PROACTIVE_TELEMETRY_ENABLE", False)):
        reg.register(Tool(
            "query_telemetry",
            "查询设备遥测历史（数值序列 + 最小/最大/均值/趋势方向/最大跳变）。"
            "用于回答『某设备某指标最近 N 小时怎么样/趋势如何/有没有异常波动』类问题。"
            "只读、不补造数据；无数据时如实返回空并给出可选设备/指标。",
            _SCHEMA_TELEMETRY, _t_query_telemetry, action_type="read", required_roles=[]))
    reg.register(Tool("draft_ticket",
                      "生成处置操作票草案（步骤/安措/风险）。诊断基本明确、需要处置步骤时调用。",
                      _SCHEMA_TASK, _t_draft_ticket, required_roles=["admin"]))
    if settings.TICKET_ACTION_LOOP_ENABLE:
        reg.register(Tool("create_ticket",
                          "把已确定的处置方案落库成两票草稿。仅当用户明确要求生成/创建工单时调用。",
                          _SCHEMA_CREATE_TICKET, _t_create_ticket,
                          required_roles=["admin", "editor"]))
        reg.register(Tool("submit_ticket",
                          "把工单提交审核（自动跑审核引擎）。create_ticket 成功后按用户要求调用。",
                          _SCHEMA_SUBMIT_TICKET, _t_submit_ticket,
                          required_roles=["admin", "editor"]))
    # H6 单代理检索循环：证据自评 + 停止准则（只读，循环里每轮调用成本很低）
    if getattr(settings, "AGENTIC_RETRIEVAL_ENABLE", False):
        reg.register(Tool(
            "assess_evidence",
            "评估当前检索证据是否足够：返回 CRAG 分级（correct/ambiguous/incorrect）、"
            "证据强度与『是否应停止检索』的建议。每轮检索后调用它决定继续还是收口作答。",
            _SCHEMA_ASSESS, _t_assess_evidence, action_type="read", required_roles=[]))
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
