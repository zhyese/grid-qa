"""系统接口：登录 / 注册 / 操作日志（角色+时间过滤） / 配置（管理员，Redis 持久化）。"""
import hmac

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limiter import limiter
from app.core.permissions import ALERT_MANAGE, ALERT_READ, AUDIT_READ, METRIC_READ, USER_MANAGE
from app.core.response import BizError, success
from app.db.session import get_db
from app.dependencies import get_current_user, require_admin, require_perm
from app.models.user import User
from app.schemas.auth import ChangePasswordRequest, LoginRequest, ProfileUpdateRequest, RegisterRequest, ResetPasswordRequest, UpdateRoleRequest, UserStatusRequest
from app.schemas.system import AlertDisposeRequest, AgentRunRequest, AiDraftUpdateRequest, ConfidenceUpdateRequest, PersonaConfigRequest, MilvusConfigRequest, ModelConfigRequest
from app.services import config_service, log_service
from app.services.alert_disposal_service import list_disposals, trigger_disposal
from app.services.persona_store import delete_config, list_configs, upsert_config
from app.services.auth_service import authenticate, change_password, delete_user, get_profile, list_users, register_user, reset_password, set_user_status, update_profile, update_user_role
from app.services.log_service import query_logs, write_log
from app.services.agent_tool_audit_service import query_tool_calls

router = APIRouter(prefix="/system", tags=["系统-用户/权限/配置"])


@router.post("/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    data = await authenticate(db, body.username, body.password)
    await write_log(db, body.username, "登录", f"用户 {body.username} 登录系统")
    return success(data, "登录成功")


# ===== 用户自助（登录即可，非管理员）=====

@router.get("/me")
async def my_profile(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """查自己的资料。"""
    return success(await get_profile(db, user.id), "查询成功")


@router.put("/me")
async def update_my_profile(
    body: ProfileUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """改自己的部门（影响文档级 ACL）。"""
    data = await update_profile(db, user.id, body.dept)
    await write_log(db, user.username, "改个人资料", f"dept → {body.dept}")
    return success(data, "已更新")


@router.put("/me/password")
async def change_my_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """自助改密码（校验旧密码）。"""
    data = await change_password(db, user.id, body.oldPassword, body.newPassword)
    await write_log(db, user.username, "改密码", "自助修改")
    return success(data, "密码已修改")


@router.post("/register")
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    data = await register_user(db, body.username, body.password, body.role, body.tenantId, body.dept)
    await write_log(db, admin.username, "注册用户", f"新增用户 {body.username}（{body.role}/{body.tenantId}/{body.dept}）")
    return success(data, "注册成功")


@router.get("/users")
async def users(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """用户列表（admin 用户管理）。"""
    data = await list_users(db, page, size)
    return success(data, "查询成功")


@router.put("/users/{user_id}/role")
async def update_role(
    user_id: str,
    body: UpdateRoleRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """改用户角色/dept（admin 用户管理）。"""
    data = await update_user_role(db, user_id, body.role, body.dept)
    await write_log(db, admin.username, "用户管理", f"{user_id} → {body.role}/{body.dept}")
    return success(data, "更新成功")


@router.patch("/users/{user_id}/status")
async def update_user_status(
    user_id: str,
    body: UserStatusRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_perm(USER_MANAGE)),
):
    """启用/禁用账号（管理员）。禁用后该用户登录被拒；不能禁用自己与最后一个管理员。"""
    data = await set_user_status(db, user_id, body.status, actor_id=admin.id)
    await write_log(db, admin.username, "用户管理", f"{user_id} → {body.status}")
    return success(data, "已更新状态")


@router.delete("/users/{user_id}")
async def delete_user_route(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_perm(USER_MANAGE)),
):
    """删除账号（管理员）。不能删自己与最后一个管理员。"""
    data = await delete_user(db, user_id, actor_id=admin.id)
    await write_log(db, admin.username, "用户管理", f"删除用户 {user_id}")
    return success(data, "已删除")


@router.post("/users/{user_id}/reset-password")
async def reset_password_route(
    user_id: str,
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_perm(USER_MANAGE)),
):
    """管理员重置用户密码。"""
    data = await reset_password(db, user_id, body.password)
    await write_log(db, admin.username, "用户管理", f"重置密码 {user_id}")
    return success(data, "密码已重置")


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


@router.get("/config/prompt")
async def get_prompt_config_route(admin: User = Depends(require_admin)):
    """读取 system prompt 覆盖（空=用 code 默认）。同时回显默认供前端对照。"""
    from app.rag.prompt_templates import SYSTEM_PROMPT
    data = await config_service.get_prompt_config()
    data["default"] = SYSTEM_PROMPT
    return success(data, "查询成功")


@router.put("/config/prompt")
async def update_prompt_config_route(
    body: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """保存 system prompt 覆盖（空串=恢复默认），即改即生效。"""
    data = await config_service.update_prompt_config(body.get("systemPrompt", ""))
    await write_log(db, admin.username, "Prompt配置", f"覆盖长度 {len(data['systemPrompt'])}")
    return success(data, "已保存（下次问答生效）")


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
    import hmac

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

    if not settings.ALERT_WEBHOOK_TOKEN:
        raise HTTPException(status_code=503, detail="告警 webhook token 未配置")
    if not hmac.compare_digest(token or "", settings.ALERT_WEBHOOK_TOKEN):
        raise HTTPException(status_code=403, detail="告警 webhook token 无效")
    try:
        body = await request.json()
    except Exception:
        body = {}
    alerts = body.get("alerts") or []
    from app.core import metrics

    ingest_failures = 0
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
        # 实时推送：广播给 /ws/alerts 订阅端（Admin 告警 Tab 不再轮询）
        try:
            from app.core import ws_manager
            await ws_manager.broadcast({"type": "alert", "severity": sev, "title": title,
                                        "summary": summary, "state": state, "time": content[-26:]})
        except Exception:
            pass
        # 统一进入“实时事件 → 持久任务 → 主动 Agent”闭环。Grafana 指纹作为幂等键，
        # resolved 事件只归档，不再触发诊断；原始 payload 完整保留用于审计。
        try:
            import hashlib

            from app.schemas.realtime_event import RealtimeDeviceRef, RealtimeEventIn
            from app.services.realtime_event_service import ingest_event

            source_device_id = str(
                labels.get("device_id") or labels.get("deviceId")
                or labels.get("instance") or ""
            )
            raw_id = "|".join((
                str(a.get("fingerprint") or title),
                str(state or ""),
                str(a.get("startsAt") or ""),
                str(a.get("endsAt") or ""),
                source_device_id,
            ))
            event_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:32]
            event_type = "recovered" if state in {"resolved", "recovered"} else "grafana_alert"
            await ingest_event(
                db,
                RealtimeEventIn(
                    eventId=event_id,
                    source="generic",
                    eventType=event_type,
                    severity=sev,
                    occurredAt=a.get("startsAt") or None,
                    title=title,
                    summary=summary,
                    device=RealtimeDeviceRef(
                        sourceDeviceId=source_device_id,
                        name=str(labels.get("device_name") or labels.get("deviceName") or ""),
                        type=str(labels.get("device_type") or labels.get("deviceType") or ""),
                        station=str(labels.get("station") or labels.get("stationName") or ""),
                    ),
                    payload={"grafana": a, "labels": labels, "annotations": ann},
                ),
                tenant_id=settings.ALERT_WEBHOOK_TENANT,
                actor="Grafana",
            )
        except Exception as e:
            from app.core.obs import degraded

            degraded("grafana_realtime_ingest", e)
            ingest_failures += 1
    if ingest_failures:
        raise HTTPException(
            status_code=503,
            detail=f"Grafana 告警接入失败 {ingest_failures}/{len(alerts)} 条，请稍后重试",
        )
    return success({"received": len(alerts)}, "告警已接收")


@router.get("/alerts")
async def alerts(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(ALERT_READ)),
):
    """告警列表（操作日志中 operate_type=告警），管理员/审计员。"""
    data = await query_logs(db, page, size, operate_type="告警")
    return success(data, "查询成功")


@router.get("/agent/tool-calls")
async def agent_tool_calls(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    persona: str | None = Query(None),
    tool: str | None = Query(None),
    username: str | None = Query(None),
    user: User = Depends(require_perm(AUDIT_READ)),
):
    """Agent 工具调用审计列表（S4，管理员/审计员）：谁/何时/哪个 persona/调了啥工具/结果。"""
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
    """S3：手动触发告警自动处置。写 pending → 持久化任务跑 ALERT_PERSONA。"""
    disp_id = await trigger_disposal(body.severity, body.title, body.summary,
                                     source="manual", model_type=body.modelType,
                                     tenant_id=admin.tenant_id)
    await write_log(db, admin.username, "告警处置", f"手动触发 {body.title[:40]} → #{disp_id}")
    return success({"id": disp_id}, "已触发处置")


@router.get("/alerts/disposals")
async def alerts_disposals(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    user: User = Depends(require_perm(ALERT_READ)),
):
    """S3：告警处置记录列表（管理员/审计员）：告警→诊断→处置→操作票草案。"""
    data = await list_disposals(page, size, status=status, tenant_id=user.tenant_id)
    return success(data, "查询成功")


# ===== ③增强：告警处置人工确认闭环 + 一键转两票（ALERT_MANAGE=admin）=====

@router.post("/alerts/disposals/{disp_id}/confirm")
@limiter.limit("10/minute")
async def disposal_confirm(request: Request, disp_id: int,
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(require_perm(ALERT_MANAGE))):
    """采纳处置预案：proposed → confirmed。"""
    from app.services.alert_disposal_service import confirm_disposal
    try:
        data = await confirm_disposal(
            db, disp_id, reviewer=user.username, tenant_id=user.tenant_id
        )
        await write_log(db, user.username, "告警处置确认", f"记录{disp_id}")
        return success(data, "已确认")
    except ValueError as e:
        raise BizError(str(e), 400)


@router.post("/alerts/disposals/{disp_id}/reject")
@limiter.limit("10/minute")
async def disposal_reject(request: Request, disp_id: int, note: str = Query(""),
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_perm(ALERT_MANAGE))):
    """驳回处置预案。"""
    from app.services.alert_disposal_service import reject_disposal
    try:
        data = await reject_disposal(
            db, disp_id, reviewer=user.username, note=note,
            tenant_id=user.tenant_id,
        )
        await write_log(db, user.username, "告警处置驳回", f"记录{disp_id} {note[:40]}")
        return success(data, "已驳回")
    except ValueError as e:
        raise BizError(str(e), 400)


@router.post("/alerts/disposals/{disp_id}/to-ticket")
@limiter.limit("10/minute")
async def disposal_to_ticket(request: Request, disp_id: int,
                             db: AsyncSession = Depends(get_db),
                             user: User = Depends(require_perm(ALERT_MANAGE))):
    """已确认预案 → 创建两票草稿（confirmed → ticketed），不自动提交审核留给人工。"""
    from app.services.alert_disposal_service import to_ticket
    try:
        data = await to_ticket(db, disp_id, creator=user.username, tenant=user.tenant_id)
        await write_log(db, user.username, "告警转两票",
                        f"记录{disp_id} → 票据{data.get('ticket', {}).get('id', '')}")
        return success(data, "已转两票草稿，请到两票管理提交审核")
    except ValueError as e:
        raise BizError(str(e), 400)


@router.post("/alerts/disposals/{disp_id}/close")
@limiter.limit("10/minute")
async def disposal_close(request: Request, disp_id: int, note: str = Query(""),
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_perm(ALERT_MANAGE))):
    """关闭处置（误报/已手动处理，无需两票）。"""
    from app.services.alert_disposal_service import close_disposal
    try:
        data = await close_disposal(
            db, disp_id, reviewer=user.username, note=note,
            tenant_id=user.tenant_id,
        )
        await write_log(db, user.username, "告警处置关闭", f"记录{disp_id}")
        return success(data, "已关闭")
    except ValueError as e:
        raise BizError(str(e), 400)


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
    user: User = Depends(require_perm(METRIC_READ)),
):
    """LLM 成本报告：今日/本月 token 消耗 + 模型分布 + 用户排行（管理员/审计员）。"""
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
    user: User = Depends(require_perm(METRIC_READ)),
):
    """检索质量分数趋势（近 N 天）（管理员/审计员）。"""
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


# ===== 数据备份与恢复（管理员；MySQL 元数据层）=====

@router.post("/backup")
async def backup_db(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """立即备份全库（MySQL 元数据）→ data/backups/mysql_*.sql。"""
    from app.services.backup_service import backup_mysql
    try:
        data = await backup_mysql()
        await write_log(db, admin.username, "数据备份", f"{data['filename']}（{data['tables']}表/{data['rows']}行）")
        return success(data, "备份成功")
    except Exception as e:
        raise BizError(f"备份失败：{type(e).__name__}: {e}"[:200], 500)


@router.get("/backups")
async def list_db_backups(admin: User = Depends(require_admin)):
    """列出全部备份文件。"""
    from app.services.backup_service import list_backups
    return success(await list_backups(), "查询成功")


@router.post("/restore")
async def restore_db(
    body: dict,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """从指定备份恢复（⚠ 覆盖当前 MySQL 数据；MinIO/Milvus 不动）。"""
    from app.services.backup_service import restore_mysql
    filename = (body or {}).get("filename", "")
    try:
        data = await restore_mysql(filename)
        await write_log(db, admin.username, "数据恢复", f"{filename}（{data['executed']}条SQL）")
        return success(data, "恢复成功")
    except BizError:
        raise
    except Exception as e:
        raise BizError(f"恢复失败：{type(e).__name__}: {e}"[:200], 500)


@router.delete("/backup")
async def delete_db_backup(
    filename: str = Query(...),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """删除一个备份文件。"""
    from app.services.backup_service import delete_backup
    data = await delete_backup(filename)
    await write_log(db, admin.username, "删除备份", filename)
    return success(data, "已删除")


# ===== 三合一备份恢复（MySQL+Redis+Milvus + 定时3h + manifest 元信息）=====

@router.post("/backup/all")
async def backup_all_db(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """一键三合一全量备份（MySQL+Redis+Milvus）→ manifest_{ts}.json。"""
    from app.services.backup_service import backup_all
    try:
        data = await backup_all()
        await write_log(db, admin.username, "全量备份",
                        f"ts={data['ts']} mysql={data['meta']['mysqlRows']}行 "
                        f"redis={data['meta']['redisKeys']}key milvus={data['meta']['milvusVectors']}向量")
        return success(data, "三合一备份成功")
    except Exception as e:
        raise BizError(f"备份失败：{type(e).__name__}: {e}"[:200], 500)


@router.post("/backup/restore-all")
async def restore_all_db(
    body: dict,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """一键恢复三合一（⚠ 全量覆盖 MySQL+Redis+Milvus）。body: {ts}。"""
    from app.services.backup_service import restore_all
    ts = (body or {}).get("ts", "")
    try:
        data = await restore_all(ts)
        await write_log(db, admin.username, "全量恢复", f"ts={ts}")
        return success(data, "三合一恢复完成")
    except BizError:
        raise
    except Exception as e:
        raise BizError(f"恢复失败：{type(e).__name__}: {e}"[:200], 500)


@router.get("/backup/list")
async def list_manifest_backups(admin: User = Depends(require_admin)):
    """列出三合一备份（manifest 元信息：时间/大小/MySQL行/Redis key/Milvus向量）。"""
    from app.services.backup_service import list_backups
    return success(await list_backups(), "查询成功")


@router.delete("/backup/all/{ts}")
async def delete_manifest_backup(
    ts: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """删除一次三合一备份（manifest + 3 数据文件）。"""
    from app.services.backup_service import delete_backup_all
    data = await delete_backup_all(ts)
    await write_log(db, admin.username, "删除全量备份", ts)
    return success(data, "已删除")


# ===== 操作日志归档（BRD §4.5.2）=====

@router.get("/logs/archive-stats")
async def logs_archive_stats(admin: User = Depends(require_admin)):
    """日志归档统计：总数/最早最晚/类型分布/超期待归档数/保留天数。"""
    from app.services.log_archive_service import archive_stats
    return success(await archive_stats(), "查询成功")


@router.post("/logs/archive")
async def logs_archive(
    body: dict,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """手动归档超期日志（导出 jsonl 再删）。body.days 可选，默认配置保留期。"""
    from app.services.log_archive_service import archive_old_logs
    days = (body or {}).get("days")
    data = await archive_old_logs(int(days) if days else None)
    await write_log(db, admin.username, "日志归档", f"归档 {data['archived']} 条 → {data.get('file')}")
    return success(data, "归档完成")


# ===== 故障预测建议（BRD §5.3.1）=====

@router.get("/fault-prediction")
async def fault_prediction(
    days: int = 30,
    user: User = Depends(require_perm(METRIC_READ)),
):
    """故障预测：聚合近 N 天告警→高频/趋势/风险分级+建议（管理员/审计员）。"""
    from app.services.fault_prediction_service import predict
    data = await predict(days)
    return success(data, "查询成功")


# ===== 双 RAG 框架热备（BRD §5.2.3）=====

@router.get("/rag/health")
async def rag_health(user: User = Depends(require_perm(METRIC_READ))):
    """主副两路健康探活：主路 Milvus 是否连通、双框架开关状态。"""
    from app.config import settings
    from app.services.rag_router import primary_health
    primary = await primary_health()
    return success({
        "dualEnable": getattr(settings, "DUAL_RAG_ENABLE", False),
        "primary": primary,
        "secondary": "BM25+LLM（always-on，独立于 Milvus）",
    }, "查询成功")


@router.get("/milvus-health")
async def milvus_health(user: User = Depends(require_perm(METRIC_READ)), db: AsyncSession = Depends(get_db)):
    """Milvus 索引健康：各 collection 向量数 vs MySQL chunk 数，检测不一致/orphan。"""
    import asyncio
    from sqlalchemy import func as _f
    from app.models.chunk import Chunk
    from app.clients import milvus_client
    from app.config import settings
    collections = {}
    for key, name in [("cloud", settings.MILVUS_COLLECTION), ("bge", settings.MILVUS_COLLECTION_BGE)]:
        try:
            n = await asyncio.to_thread(milvus_client.num_entities, name)
        except Exception as e:
            n = -1
            from app.core.obs import degraded
            degraded("milvus_health", e)
        collections[key] = {"collection": name, "vectors": n}
    mysql_chunks = (await db.execute(select(_f.count()).select_from(Chunk))).scalar() or 0
    cloud_vec = collections["cloud"]["vectors"]
    return success({
        "collections": collections,
        "mysqlChunks": mysql_chunks,
        "consistent": cloud_vec >= 0 and abs(cloud_vec - mysql_chunks) <= max(5, mysql_chunks * 0.01),
        "note": "cloud 向量数应 ≈ MySQL chunk 数（bge 为小文档副本，通常更少）",
    }, "查询成功")


# ===== 插件 / 扩展框架（BRD §5.3.1）=====

@router.get("/plugins")
async def plugins_list(user: User = Depends(require_perm(METRIC_READ))):
    """列出全部插件及启用状态。"""
    from app.services.plugin_registry import list_plugins
    return success(list_plugins(), "查询成功")


@router.post("/plugins/{name}/toggle")
async def plugin_toggle(
    name: str,
    body: dict,
    user: User = Depends(require_perm(METRIC_READ)),
):
    """启用/禁用插件。body: {enabled: bool}。"""
    from app.services.plugin_registry import disable, enable
    ok = enable(name) if (body or {}).get("enabled") else disable(name)
    if not ok:
        raise BizError(f"插件 {name} 不存在", 404)
    return success({"name": name, "enabled": (body or {}).get("enabled", False)}, "已更新")


# ===== 语义增强规则自定义（BRD §4.1.3）=====

@router.get("/semantic-rules")
async def sem_rules_list(admin: User = Depends(require_admin)):
    """列全部语义增强规则（维度→关键词→标签）。"""
    from app.services.semantic_rule_service import list_rules
    return success(list_rules(), "查询成功")


@router.post("/semantic-rules")
async def sem_rules_add(body: dict, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """新增规则 {dimension, tag, keywords:[]}。"""
    from app.services.semantic_rule_service import add_rule
    data = add_rule(body.get("dimension", ""), body.get("tag", ""), body.get("keywords", []))
    await write_log(db, admin.username, "语义规则", f"{data['dimension']}→{data['tag']}")
    return success(data, "已保存")


@router.delete("/semantic-rules/{idx}")
async def sem_rules_del(idx: int, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """按下标删除规则。"""
    from app.services.semantic_rule_service import delete_rule
    data = delete_rule(idx)
    await write_log(db, admin.username, "语义规则", f"删除#{idx}")
    return success(data, "已删除")


# ===== 告警实时推送 WebSocket（BRD §5.4 + 告警实时性）=====

@router.websocket("/ws/alerts")
async def alerts_ws(ws: WebSocket):
    """告警实时推送订阅。?token=JWT 鉴权；webhook 收到告警即广播。"""
    from app.core.security import decode_token
    from app.core import ws_manager
    token = ws.query_params.get("token", "")
    try:
        decode_token(token)
    except Exception:
        await ws.accept()
        await ws.send_json({"type": "error", "message": "未认证或 token 无效"})
        await ws.close()
        return
    await ws_manager.connect(ws)
    try:
        await ws.send_json({"type": "ready", "clients": ws_manager.client_count()})
        while True:
            await ws.receive_text()   # 保活（客户端可周期 ping）
    except Exception:
        pass
    finally:
        ws_manager.disconnect(ws)


# ===== 词表管理（BRD §4.1.4）=====

@router.get("/terms")
async def terms_list(admin: User = Depends(require_admin)):
    """列全部术语词条（alias→standard）。"""
    from app.services.term_service import list_terms
    return success(list_terms(), "查询成功")


@router.post("/terms")
async def terms_add(
    body: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """新增/更新一条术语（{alias, standard}），立即生效（缓存清）。"""
    from app.services.term_service import add_term
    data = add_term(body.get("alias", ""), body.get("standard", ""))
    await write_log(db, admin.username, "词表管理", f"{data['alias']}→{data['standard']}")
    return success(data, "已保存")


@router.delete("/terms")
async def terms_delete(
    alias: str = Query(...),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除一条术语。"""
    from app.services.term_service import delete_term
    data = delete_term(alias)
    await write_log(db, admin.username, "词表管理", f"删除 {alias}")
    return success(data, "已删除")
