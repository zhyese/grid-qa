"""系统接口：登录 / 注册 / 操作日志（角色+时间过滤） / 配置（管理员，Redis 持久化）。"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import BizError, success
from app.db.session import get_db
from app.dependencies import get_current_user, require_admin
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest
from app.schemas.system import AlertDisposeRequest, AgentRunRequest, AiDraftUpdateRequest, ConfidenceUpdateRequest, PersonaConfigRequest, MilvusConfigRequest, ModelConfigRequest
from app.services import config_service, log_service
from app.services.alert_disposal_service import list_disposals, trigger_disposal
from app.services.persona_store import delete_config, list_configs, upsert_config
from app.services.auth_service import authenticate, register_user
from app.services.log_service import query_logs, write_log
from app.services.agent_tool_audit_service import query_tool_calls

router = APIRouter(prefix="/system", tags=["系统-用户/权限/配置"])


@router.post("/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    data = await authenticate(db, body.username, body.password)
    await write_log(db, body.username, "登录", f"用户 {body.username} 登录系统")
    return success(data, "登录成功")


@router.post("/register")
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    data = await register_user(db, body.username, body.password, body.role, body.tenantId)
    await write_log(db, admin.username, "注册用户", f"新增用户 {body.username}（{body.role}/{body.tenantId}）")
    return success(data, "注册成功")


@router.get("/logs")
async def logs(
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    startTime: str = Query(None),
    endTime: str = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    operate_user = None if user.role == "admin" else user.username
    data = await query_logs(
        db, page, size, operate_user=operate_user, start_time=startTime, end_time=endTime
    )
    return success(data, "查询成功")


@router.post("/config/milvus")
async def config_milvus(body: MilvusConfigRequest, admin: User = Depends(require_admin)):
    data = await config_service.update_milvus_config(body.indexType, body.param)
    return success(data, "配置成功")


@router.post("/config/model")
async def config_model(body: ModelConfigRequest, admin: User = Depends(require_admin)):
    data = await config_service.update_model_config(body.modelType, body.param)
    return success(data, "配置成功")


@router.get("/config/milvus")
async def get_milvus_config_route(admin: User = Depends(require_admin)):
    return success(await config_service.get_milvus_config(), "查询成功")


@router.get("/config/model")
async def get_model_config_route(admin: User = Depends(require_admin)):
    return success(await config_service.get_model_config(), "查询成功")


@router.get("/health/providers")
async def health_providers(admin: User = Depends(require_admin)):
    """主动探测当前 LLM/Embedding provider 是否真实可用（抓欠费/配额/key失效/网络问题）。

    会消耗少量 token（LLM ping + embed 一条短文本），按需调用；常规探活用 GET /health（只看配置态）。
    """
    from app.providers.factory import check_embedding_health, check_llm_health

    llm = await check_llm_health()
    emb = await check_embedding_health()
    all_ok = llm["status"] == "ok" and emb["status"] == "ok"
    return success(
        {
            "status": "healthy" if all_ok else "degraded",
            "llm": llm,
            "embedding": emb,
        },
        "provider 探测完成",
    )


@router.get("/config/nacos")
async def nacos_config_route(admin: User = Depends(require_admin)):
    """拉取 Nacos 配置中心配置（测试连通 + 查看覆盖项）。"""
    from app.clients.nacos_client import fetch_config
    from app.config import settings

    try:
        cfg = await fetch_config()
        return success(
            {"server": settings.NACOS_SERVER, "dataId": settings.NACOS_DATA_ID,
             "group": settings.NACOS_GROUP, "items": len(cfg), "config": cfg},
            "拉取成功",
        )
    except Exception as e:
        return success(
            {"server": settings.NACOS_SERVER, "error": f"{type(e).__name__}: {e}"[:200]},
            "拉取失败（确认 nacos 已启动：docker compose up -d nacos）",
        )


@router.post("/alerts/webhook")
async def alerts_webhook(
    request: Request,
    token: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Grafana alerting contact point 回调：接收告警 → 落操作日志(operate_type=告警) + 计指标。

    免 JWT 鉴权（Grafana webhook 不带我们的 token），改用共享密钥 query 校验防滥发。
    落库后管理员在「系统管理 → 操作日志/告警」直接看到，形成"指标→告警→可见"闭环，
    不依赖外部钉钉/企微凭据。payload 取 Grafana 标准 alertmanager 风格 {alerts:[...]}。
    """
    from app.config import settings

    if token != settings.ALERT_WEBHOOK_TOKEN:
        raise BizError("告警 webhook token 无效", 403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    alerts = body.get("alerts") or []
    from app.core import metrics

    for a in alerts:
        labels = a.get("labels") or {}
        ann = a.get("annotations") or {}
        sev = labels.get("severity", "warning")
        title = labels.get("alertname", "未知告警")
        summary = ann.get("summary", "")
        state = a.get("status", "firing")
        content = f"[{sev}] {title}" + (f"：{summary}" if summary else "") + f"（{state}）"
        await write_log(db, "Grafana", "告警", content[:500])
        try:
            metrics.ALERT_RECEIVED.labels(sev).inc()
        except Exception:
            pass
        # S3：触发自动处置（写 pending 快，disposal 跑在 bg task，不阻塞 webhook 响应）
        try:
            await trigger_disposal(sev, title, summary, source="webhook")
        except Exception:
            pass
    return success({"received": len(alerts)}, "告警已接收")


@router.get("/alerts")
async def alerts(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """告警列表（操作日志中 operate_type=告警），管理员。"""
    data = await query_logs(db, page, size, operate_type="告警")
    return success(data, "查询成功")


@router.get("/agent/tool-calls")
async def agent_tool_calls(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    persona: str | None = Query(None),
    tool: str | None = Query(None),
    username: str | None = Query(None),
    admin: User = Depends(require_admin),
):
    """Agent 工具调用审计列表（S4，管理员）：谁/何时/哪个 persona/调了啥工具/结果。"""
    data = await query_tool_calls(page, size, persona=persona, tool=tool, username=username)
    return success(data, "查询成功")


@router.post("/agent/run")
async def agent_run(
    body: AgentRunRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """通用 agent 入口：按 persona 名跑 run_agent（支持自定义 DB persona）。返回 answer+steps。"""
    from app.services.persona_store import get_persona
    from app.services.agent_runtime import run_agent
    persona = await get_persona(body.persona)
    if persona is None:
        raise BizError(f"persona '{body.persona}' 不存在", 404)
    result = await run_agent(db, persona, body.query, body.modelType,
                             ctx={"username": user.username, "tenant": user.tenant_id})
    return success({
        "persona": body.persona,
        "answer": result.answer if isinstance(result.answer, str) else result.answer,
        "steps": result.steps, "iterations": result.iterations,
        "degraded": result.degraded, "toolsUsed": result.tools_used,
        "latencyMs": result.latency_ms,
    }, "完成")


@router.post("/alerts/dispose")
async def alerts_dispose(
    body: AlertDisposeRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """S3：手动触发告警自动处置（演示/测试用）。写 pending → bg task 跑 ALERT_PERSONA。"""
    disp_id = await trigger_disposal(body.severity, body.title, body.summary,
                                     source="manual", model_type=body.modelType)
    await write_log(db, admin.username, "告警处置", f"手动触发 {body.title[:40]} → #{disp_id}")
    return success({"id": disp_id}, "已触发处置")


@router.get("/alerts/disposals")
async def alerts_disposals(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    admin: User = Depends(require_admin),
):
    """S3：告警处置记录列表（admin）：告警→诊断→处置→操作票草案。"""
    data = await list_disposals(page, size, status=status)
    return success(data, "查询成功")


@router.get("/agent/personas")
async def agent_personas(admin: User = Depends(require_admin)):
    """S5：persona 配置列表（code persona 清单 + DB 覆盖配置）。"""
    data = await list_configs()
    return success(data, "查询成功")


@router.post("/agent/personas")
async def agent_persona_upsert(
    body: PersonaConfigRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """S5：新增/更新 persona 配置（DB 覆盖 code 的 prompt/工具/参数，fallback 保留 code）。"""
    data = await upsert_config(body.name, body.systemPrompt, body.allowedTools,
                               body.maxIter, body.temperature, body.maxTokens,
                               body.outputFormat, body.enabled, body.fallbackKey or "")
    await write_log(db, admin.username, "persona配置", f"{body.name} enabled={body.enabled}")
    return success(data, "保存成功")


@router.delete("/agent/personas")
async def agent_persona_delete(
    name: str = Query(...),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """S5：删除 persona DB 覆盖配置（恢复 code 默认）。"""
    data = await delete_config(name)
    await write_log(db, admin.username, "persona配置", f"删除 {name}")
    return success(data, "删除成功")


# ===== 反馈驱动优化闭环 =====


@router.get("/optimizer/report")
async def optimizer_report(
    admin: User = Depends(require_admin),
):
    """获取反馈驱动优化建议报告（缓存文件，非实时）。"""
    from app.services.feedback_optimizer_service import get_optimization_report
    report = await get_optimization_report()
    return success(report, "查询成功")


@router.post("/optimizer/generate")
async def optimizer_generate(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """实时生成反馈驱动优化建议报告（分析 dislike 模式 → 知识盲区 → 缓存建议）。"""
    from app.services.feedback_optimizer_service import generate_optimization_report
    report = await generate_optimization_report(db)
    await write_log(db, admin.username, "生成优化报告", f"分析：{report['totalDislike']}次dislike → {report['suggestionCount']}条建议")
    return success(report, "生成成功")


@router.post("/optimizer/tune-cache")
async def optimizer_tune_cache(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """缓存 TTL 自动调优：高频坏答案(dislike≥3)进黑名单禁缓存，高频好答案(like≥5)标延长候选。"""
    from app.services.feedback_optimizer_service import auto_tune_cache_ttl
    res = await auto_tune_cache_ttl(db)
    await write_log(db, admin.username, "缓存TTL调优",
                    f"黑名单{res['appliedBlacklist']}条，延长候选{len(res['extended'])}条")
    return success(res, "调优完成")


@router.get("/optimizer/blacklist")
async def optimizer_blacklist_list(
    admin: User = Depends(require_admin),
):
    """列出当前缓存黑名单（归一化 query）。"""
    from app.services.feedback_optimizer_service import list_blacklist
    return success(await list_blacklist(), "查询成功")


@router.post("/optimizer/blacklist")
async def optimizer_blacklist_add(
    query: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """手动加入缓存黑名单（query 走 query param）。"""
    from app.services.feedback_optimizer_service import add_blacklist
    nq = await add_blacklist(query)
    await write_log(db, admin.username, "加黑名单", nq[:60])
    return success({"query": nq}, "已加入黑名单")


@router.delete("/optimizer/blacklist")
async def optimizer_blacklist_remove(
    query: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """手动移出缓存黑名单。"""
    from app.services.feedback_optimizer_service import remove_blacklist
    nq = await remove_blacklist(query)
    await write_log(db, admin.username, "移除黑名单", nq[:60])
    return success({"query": nq}, "已移出黑名单")


@router.get("/optimizer/rewrite-stats")
async def optimizer_rewrite_stats(
    period: str = "today",
    admin: User = Depends(require_admin),
):
    """Query 改写质量评估统计（总数/采纳率/缓存命中/策略分布），驱动可视化面板。"""
    from app.services.rewrite_event_service import stats
    return success(await stats(period), "查询成功")


@router.get("/optimizer/rewrite-events")
async def optimizer_rewrite_events(
    page: int = 1, size: int = 20, strategy: str | None = None,
    adopted: bool | None = None,
    admin: User = Depends(require_admin),
):
    """Query 改写事件明细（可按 strategy/adopted 过滤），供面板散点/明细表。"""
    from app.services.rewrite_event_service import events_page
    return success(await events_page(page, size, strategy, adopted), "查询成功")


# ===== 证据补全闭环 =====

@router.get("/evidence-gap")
async def evidence_gap_list(
    status: str | None = None, page: int = 1, size: int = 20,
    admin: User = Depends(require_admin),
):
    """证据补全列表（按 status 过滤）。"""
    from app.services.evidence_gap_service import list_gaps
    return success(await list_gaps(status, page, size), "查询成功")


@router.post("/evidence-gap/{gap_id}/ai-draft")
async def evidence_gap_ai_draft(
    gap_id: int, model_type: str | None = None,
    admin: User = Depends(require_admin),
):
    """AI 续写草稿（放宽检索再生成）。"""
    from app.services.evidence_gap_service import ai_draft
    draft = await ai_draft(gap_id, model_type)
    return success({"aiDraft": draft}, "续写完成")


@router.put("/evidence-gap/{gap_id}/ai-draft")
async def evidence_gap_update_ai_draft(
    gap_id: int, body: AiDraftUpdateRequest,
    admin: User = Depends(require_admin),
):
    """就地编辑保存 AI 草稿（点击草稿文本直接改，失焦保存）。"""
    from app.services.evidence_gap_service import update_ai_draft
    data = await update_ai_draft(gap_id, body.aiDraft)
    return success(data, "草稿已保存")


@router.put("/evidence-gap/{gap_id}/confidence")
async def evidence_gap_update_confidence(
    gap_id: int, body: ConfidenceUpdateRequest,
    admin: User = Depends(require_admin),
):
    """后台标注 confidence（如 refused→充足 sufficient，admin 人工修正）。"""
    from app.services.evidence_gap_service import update_confidence
    data = await update_confidence(gap_id, body.confidence)
    return success(data, "标注已保存")


@router.post("/evidence-gap/retag")
async def evidence_gap_retag(admin: User = Depends(require_admin)):
    """一次性补全旧 FAQ 的设备标签（扫 equipment_tags 空的 FAQ，从 Chunk content 提取）。"""
    from app.services.evidence_gap_service import retag_faq_equipment
    data = await retag_faq_equipment()
    return success(data, "补全完成")


@router.post("/evidence-gap/{gap_id}/deep-draft")
async def evidence_gap_deep_draft(
    gap_id: int, model_type: str | None = None,
    admin: User = Depends(require_admin),
):
    """深度AI补全（SSE 流式）：meta→tool_step×N→token→done，实时看 Agent 思考链 + 答案生成。"""
    import json as _json
    from fastapi.responses import StreamingResponse
    from app.services.evidence_gap_service import deep_draft_stream

    async def gen():
        async for ev in deep_draft_stream(gap_id, model_type):
            yield f"data: {_json.dumps(ev, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/evidence-gap/{gap_id}/edit")
async def evidence_gap_edit(
    gap_id: int, body: dict,
    admin: User = Depends(require_admin),
):
    """人工编辑最终答案（status→confirmed，不触发同步，待「确认同步」）。"""
    from app.services.evidence_gap_service import edit_answer
    r = await edit_answer(gap_id, body.get("finalAnswer", ""))
    return success(r, "已保存" if r.get("ok") else "保存失败")


@router.post("/evidence-gap/{gap_id}/confirm")
async def evidence_gap_confirm(
    gap_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """确认同步：用已编辑的 final_answer（或 AI草稿兜底）同步入库。"""
    from app.services.evidence_gap_service import confirm_and_sync, get_gap
    g = await get_gap(gap_id)
    if not g:
        return success({"ok": False, "msg": "记录不存在"}, "失败")
    final = (g.get("finalAnswer") or "").strip() or (g.get("aiDraft") or "").strip()
    if not final:
        return success({"ok": False, "msg": "请先人工编辑或 AI续写"}, "失败")
    r = await confirm_and_sync(gap_id, final, admin.username)
    await log_service.write_log(db, admin.username, "证据补全确认同步", f"gap#{gap_id} ok={r.get('ok')}")
    return success(r, "已确认并同步入库" if r.get("ok") else "同步失败")


@router.post("/evidence-gap/{gap_id}/ignore")
async def evidence_gap_ignore(
    gap_id: int, db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """忽略（不补，status=ignored）。"""
    from app.models.evidence_gap import EvidenceGap
    row = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
    if row:
        row.status = "ignored"
        await db.commit()
    return success(None, "已忽略")


@router.delete("/evidence-gap/{gap_id}")
async def evidence_gap_delete(
    gap_id: int, db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """删除一条证据补全记录。"""
    from app.models.evidence_gap import EvidenceGap
    row = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return success(None, "已删除")


# ===== P2-⑦ LLM 成本追踪 =====


@router.get("/cost/report")
async def cost_report(
    period: str = "today",
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """LLM 成本报告：今日/本月 token 消耗 + 模型分布 + 用户排行。"""
    from app.services.cost_tracker_service import get_cost_report
    report = await get_cost_report(db, period)
    return success(report, "查询成功")


@router.get("/cost/quota")
async def cost_quota(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查询当前用户配额使用情况。"""
    from app.services.cost_tracker_service import check_quota
    result = await check_quota(user.username, user.tenant_id)
    return success(result, "查询成功")


# ===== P2-⑩ 在线质量评测 =====


@router.get("/eval/trends")
async def eval_trends(
    days: int = 7,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """检索质量分数趋势（近 N 天）。"""
    from app.services.online_eval_service import get_quality_trends
    trends = await get_quality_trends(db, days)
    return success(trends, "查询成功")


@router.get("/routing/config")
async def routing_config_get(
    admin: User = Depends(require_admin),
):
    """查看路由配置 & A/B 测试状态。"""
    from app.routing.config import router_config
    return success({
        "enabled": router_config.enabled,
        "sparseMaxLen": router_config.sparse_max_len,
        "denseMinLen": router_config.dense_min_len,
        "minConfidence": router_config.min_confidence,
        "abTestRatio": router_config.ab_test_ratio,
        "hybridRoutes": list(router_config.hybrid_routes),
    }, "查询成功")


@router.get("/knowledge/quality")
async def knowledge_quality(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """P3-⑬ 知识库质量分 & 盲区诊断。"""
    from app.services.knowledge_quality_service import score_knowledge_quality
    report = await score_knowledge_quality(db)
    return success(report, "查询成功")
