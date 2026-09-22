"""问答接口：智能问答(普通/流式/多轮) / 对话历史 / 反馈 / 术语归一化。"""
import asyncio
import json
import time

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limiter import limiter
from app.core.response import success
from app.db.session import get_db
from app.core.permissions import FEEDBACK_MANAGE, FEEDBACK_READ, QA_ANSWER
from app.dependencies import get_current_user, require_perm
from app.models.user import User
from app.schemas.qa import (
    BatchDeleteRequest,
    ExportRequest,
    FaithfulnessRequest,
    FeedbackRequest,
    QaAnswerRequest,
    RelatedRequest,
    RenameRequest,
    TermRequest,
)
from app.services import conversation_service, favorite_service, feedback_service, qa_service, term_service
from app.services.log_service import write_log

router = APIRouter(prefix="/qa", tags=["检索与问答"])

# 持有后台异步任务引用，防 GC 回收
_bg_tasks: set = set()


@router.post("/answer")
@limiter.limit("30/minute")
async def answer(
    request: Request,
    body: QaAnswerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(QA_ANSWER)),
    regen: bool = False,   # 与 /answer/stream 对齐：跳过缓存读强制重生成（此前仅流式支持）
):
    from app.config import settings
    from app.services import plugin_registry
    from app.core.qa_trace import new_collector
    from app.core.llm_user_observer import bind_identity, emit
    # 链路 trace：请求入口建 collector（绑 contextvar，下游 answer/mixed_search 自动打点）
    _trace_c = new_collector(body.query) if getattr(settings, "QA_TRACE_ENABLE", True) else None
    bind_identity(
        username=user.username, tenant=user.tenant_id or "default",
        conversation_id=body.conversationId or "",
        qa_trace_id=_trace_c.trace_id if _trace_c else "",
    )
    if body.conversationId:
        emit(
            "conversation.continued", {"channel": "http"}, username=user.username,
            tenant=user.tenant_id or "default", conversation_id=body.conversationId,
            qa_trace_id=_trace_c.trace_id if _trace_c else "",
        )
    emit(
        "qa.started",
        {"query": body.query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "", "modelType": body.modelType or "", "agentMode": body.agentMode},
        username=user.username, tenant=user.tenant_id or "default",
        conversation_id=body.conversationId or "", qa_trace_id=_trace_c.trace_id if _trace_c else "",
    )
    # 插件 · query 预处理（BRD §5.3.1 扩展点）
    q = plugin_registry.run_hook("query_preprocess", body.query, {"user": user.username})
    if getattr(settings, "DUAL_RAG_ENABLE", False):
        # 双 RAG 热备：主路异常自动切副路 BM25+LLM（BRD §5.2.3）
        from app.services.rag_router import answer_redundant
        data = await answer_redundant(
            db, q, body.modelType, conversation_id=body.conversationId,
            username=user.username, tenant=user.tenant_id,
            user_dept=user.dept, user_role=user.role,
        )
    else:
        data = await qa_service.answer(
            db, q, body.modelType, conversation_id=body.conversationId,
            username=user.username, tenant=user.tenant_id,
            user_dept=user.dept, user_role=user.role, regen=regen,
        )
    # 插件 · 答案后处理（扩展点）
    if isinstance(data, dict) and data.get("answer"):
        data["answer"] = plugin_registry.run_hook("answer_postprocess", data["answer"], {"query": q})
    # X-Cache-Hit 响应头：供 HTTP 层面调试/监控缓存分层命中了哪层
    layer = data.get("cacheLayer") or data.get("cached") and "redis" or "llm"
    request.state.cache_layer = layer
    # 链路 trace：注入响应（实时内嵌）+ 异步落库（采样；失败绝不影响主链路）
    if _trace_c is not None:
        try:
            data["trace"] = _trace_c.to_dict()
            from app.services.qa_trace_service import save_trace
            _bg_tasks.add(asyncio.create_task(save_trace(
                data["trace"], query=q, tenant=user.tenant_id or "default",
                username=user.username, cache_layer=data.get("cacheLayer", ""),
                confidence=data.get("confidence", ""))))
        except Exception as e:
            from app.core.obs import degraded
            degraded("qa_trace_attach", e)
    await write_log(db, user.username, "智能问答", f"提问：{body.query[:50]}")
    if not body.conversationId and data.get("conversationId"):
        emit(
            "conversation.started", {"channel": "http"}, username=user.username,
            tenant=user.tenant_id or "default", conversation_id=data.get("conversationId", ""),
            qa_trace_id=_trace_c.trace_id if _trace_c else "",
        )
    emit(
        "qa.completed",
        {
            "query": body.query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
            "answer": data.get("answer", "") if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
            "modelType": data.get("modelType", body.modelType or ""),
            "route": data.get("route", ""), "cached": bool(data.get("cached")),
            "cacheLayer": data.get("cacheLayer", ""), "confidence": data.get("confidence", ""),
            "degraded": bool(data.get("llmDegraded") or data.get("retrievalDegraded")),
            "degradationReason": data.get("llmDegradedReason", data.get("retrievalDegradedReason", "")),
            "faithfulness": 1 - data.get("hallucinationRate", 0) if isinstance(data.get("hallucinationRate"), (int, float)) else "",
            "request": {
                "query": body.query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
                "modelType": body.modelType, "conversationId": body.conversationId,
            },
        },
        username=user.username, tenant=user.tenant_id or "default",
        conversation_id=data.get("conversationId", body.conversationId or ""),
        qa_trace_id=_trace_c.trace_id if _trace_c else "",
    )
    return success(data, "问答成功")


@router.post("/answer/stream")
@limiter.limit("30/minute")
async def answer_stream(
    request: Request,
    body: QaAnswerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(QA_ANSWER)),
    regen: bool = False,
):
    from app.config import settings
    from app.core.llm_user_observer import bind_identity, emit
    from app.core.qa_trace import new_collector

    _trace_c = new_collector(body.query) if getattr(settings, "QA_TRACE_ENABLE", True) else None
    bind_identity(
        username=user.username, tenant=user.tenant_id or "default",
        conversation_id=body.conversationId or "", qa_trace_id=_trace_c.trace_id if _trace_c else "",
    )
    if body.conversationId:
        emit(
            "conversation.continued", {"channel": "sse"}, username=user.username,
            tenant=user.tenant_id or "default", conversation_id=body.conversationId,
            qa_trace_id=_trace_c.trace_id if _trace_c else "",
        )
    emit(
        "qa.stream.started",
        {"query": body.query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "", "modelType": body.modelType or "", "agentMode": body.agentMode,
         "request": {
             "query": body.query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
             "modelType": body.modelType, "conversationId": body.conversationId,
         }},
        username=user.username, tenant=user.tenant_id or "default",
        conversation_id=body.conversationId or "", qa_trace_id=_trace_c.trace_id if _trace_c else "",
    )

    async def gen():
        token_count = 0
        answer_parts: list[str] = []
        answer_chars = 0
        first_token_ms = None
        started_at = time.perf_counter()
        completed = False
        terminal_emitted = False
        conversation_id = body.conversationId or ""
        conversation_started_emitted = bool(body.conversationId)
        try:
            async for item in qa_service.stream_answer(
                db, body.query, body.modelType,
                conversation_id=body.conversationId, username=user.username, tenant=user.tenant_id,
                regen=regen, agent_mode=body.agentMode,
                user_dept=user.dept, user_role=user.role,
                memory_read=body.memoryRead, memory_write=body.memoryWrite,
                memory_scope=body.memoryScope,
                trace_id=_trace_c.trace_id if _trace_c else "",
            ):
                if isinstance(item, dict):
                    if item.get("type") == "token":
                        token_count += 1
                        if settings.LLM_USER_OBSERVER_CAPTURE_TEXT and answer_chars < 16000:
                            content = str(item.get("content", ""))[:16000 - answer_chars]
                            answer_parts.append(content)
                            answer_chars += len(content)
                        if first_token_ms is None:
                            first_token_ms = round((time.perf_counter() - started_at) * 1000, 3)
                            emit(
                                "qa.stream.first_token", {"firstTokenMs": first_token_ms},
                                username=user.username, tenant=user.tenant_id or "default",
                                conversation_id=conversation_id,
                                qa_trace_id=_trace_c.trace_id if _trace_c else "",
                            )
                    conversation_id = item.get("conversationId") or conversation_id
                    if conversation_id and not conversation_started_emitted:
                        emit(
                            "conversation.started", {"channel": "sse"}, username=user.username,
                            tenant=user.tenant_id or "default", conversation_id=conversation_id,
                            qa_trace_id=_trace_c.trace_id if _trace_c else "",
                        )
                        conversation_started_emitted = True
                    if item.get("type") == "error" and not terminal_emitted:
                        emit(
                            "qa.stream.error",
                            {
                                "query": body.query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
                                "errorType": str(item.get("errorType") or item.get("code") or "stream_error"),
                                "tokenEvents": token_count,
                            },
                            username=user.username, tenant=user.tenant_id or "default",
                            conversation_id=conversation_id,
                            qa_trace_id=_trace_c.trace_id if _trace_c else "",
                        )
                        terminal_emitted = True
                # done 事件统一注入 trace（stream_answer 内部已打点）+ 异步落库
                if isinstance(item, dict) and item.get("type") == "done" and not terminal_emitted:
                    if _trace_c is not None:
                        try:
                            item["trace"] = _trace_c.to_dict()
                            from app.services.qa_trace_service import save_trace
                            _bg_tasks.add(asyncio.create_task(save_trace(
                                item["trace"], query=body.query, tenant=user.tenant_id or "default",
                                username=user.username, cache_layer=item.get("cacheLayer", ""),
                                confidence=item.get("confidence", ""))))
                        except Exception as e:
                            from app.core.obs import degraded
                            degraded("qa_trace_attach_stream", e)
                    completed = True
                    emit(
                        "qa.stream.completed",
                        {
                            "query": body.query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
                            "answer": (
                                item.get("content") or item.get("annotatedAnswer") or "".join(answer_parts)
                            ) if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
                            "tokenEvents": token_count, "firstTokenMs": first_token_ms,
                            "modelType": item.get("modelType", body.modelType or ""),
                            "route": item.get("route", ""), "cached": bool(item.get("cached")),
                            "cacheLayer": item.get("cacheLayer", ""), "confidence": item.get("confidence", ""),
                            "degraded": bool(item.get("llmDegraded") or item.get("retrievalDegraded")),
                            "degradationReason": item.get("llmDegradedReason", item.get("retrievalDegradedReason", "")),
                            "faithfulness": 1 - item.get("hallucinationRate", 0) if isinstance(item.get("hallucinationRate"), (int, float)) else "",
                        },
                        username=user.username, tenant=user.tenant_id or "default",
                        conversation_id=conversation_id, qa_trace_id=_trace_c.trace_id if _trace_c else "",
                    )
                    terminal_emitted = True
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            if not terminal_emitted:
                emit("qa.stream.aborted", {"query": body.query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "", "tokenEvents": token_count}, username=user.username,
                     tenant=user.tenant_id or "default", conversation_id=conversation_id,
                     qa_trace_id=_trace_c.trace_id if _trace_c else "")
                terminal_emitted = True
            raise
        except Exception as exc:
            if not terminal_emitted:
                emit("qa.stream.error", {"query": body.query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "", "errorType": type(exc).__name__, "tokenEvents": token_count},
                     username=user.username, tenant=user.tenant_id or "default", conversation_id=conversation_id,
                     qa_trace_id=_trace_c.trace_id if _trace_c else "")
                terminal_emitted = True
            raise
        finally:
            if not completed and not terminal_emitted:
                emit("qa.stream.aborted", {"query": body.query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "", "tokenEvents": token_count}, username=user.username,
                     tenant=user.tenant_id or "default", conversation_id=conversation_id,
                     qa_trace_id=_trace_c.trace_id if _trace_c else "")

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/trace")
@limiter.limit("60/minute")
async def list_trace(
    request: Request,
    page: int = 1,
    size: int = 20,
    slowMs: float | None = None,
    bottleneck: str = "",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(QA_ANSWER)),
):
    """链路诊断·历史列表：可过滤慢查询(slowMs)/瓶颈节点。admin 查全租户，他人仅查自己。"""
    from app.services.qa_trace_service import list_traces
    username = "" if user.role == "admin" else user.username
    data = await list_traces(tenant=user.tenant_id, page=page, size=size,
                             slow_ms=slowMs, bottleneck=bottleneck, username=username)
    return success(data, "查询成功")


@router.get("/trace/{trace_id}")
async def get_trace_api(
    trace_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(QA_ANSWER)),
):
    """链路诊断·单条明细：含各阶段 spans（前端复用 QaTraceChart 渲染瀑布图）。"""
    from app.core.response import BizError
    from app.services.qa_trace_service import get_trace
    data = await get_trace(trace_id, tenant=user.tenant_id)
    if not data:
        raise BizError("链路记录不存在", 404)
    return success(data, "查询成功")


@router.get("/conversations")
async def conversations(
    keyword: str = "",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await conversation_service.list_conversations(db, user.username, keyword=keyword)
    return success(data, "查询成功")


@router.put("/conversations/{conv_id}")
async def rename_conv(
    conv_id: str,
    body: RenameRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ok = await conversation_service.rename_conversation(db, user.username, conv_id, body.title)
    return success({"renamed": ok}, "重命名成功" if ok else "对话不存在或无权限")


@router.delete("/conversations/{conv_id}")
async def delete_conv(
    conv_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ok = await conversation_service.delete_conversation(db, user.username, conv_id)
    return success({"deleted": ok}, "删除成功" if ok else "对话不存在或无权限")


@router.post("/conversations/batch-delete")
@limiter.limit("30/minute")
async def batch_delete_convs(
    request: Request,
    body: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量软删会话（DB 行保留，列表过滤）。仅删本人会话。"""
    n = await conversation_service.batch_delete_conversations(db, user.username, body.ids)
    await write_log(db, user.username, "批量删除会话", f"{n} 条")
    return success({"deleted": n}, f"已删除 {n} 条")


@router.post("/messages/batch-delete")
@limiter.limit("30/minute")
async def batch_delete_msgs(
    request: Request,
    body: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量软删消息（DB 行保留，历史过滤）。仅删本人会话下消息。"""
    n = await conversation_service.batch_delete_messages(db, user.username, body.ids)
    await write_log(db, user.username, "批量删除消息", f"{n} 条")
    return success({"deleted": n}, f"已删除 {n} 条")


@router.get("/history")
async def history(
    conversationId: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await conversation_service.get_messages(db, conversationId, limit=100)
    return success(data, "查询成功")


@router.post("/term/normalize")
async def term_normalize(
    body: TermRequest,
    user: User = Depends(get_current_user),
):
    data = {
        "originalTerm": body.term,
        "normalizedTerm": term_service.normalize(body.term),
        "explanation": "",
    }
    return success(data, "归一化成功")


@router.post("/feedback")
@limiter.limit("60/minute")
async def feedback(
    request: Request,
    body: FeedbackRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """问答反馈（👍/👎），沉淀坏 case；dislike 自动异步打 judge 分 + 缓存失效。"""
    from app.core.trace_id import resolve_trace_id

    tenant_id = getattr(user, "tenant_id", None) or "default"
    trace_id = resolve_trace_id(body.traceId)
    await feedback_service.record_feedback(
        db, conversation_id=body.conversationId or "", query=body.query,
        answer=body.answer, feedback=body.feedback, username=user.username,
        reason=body.reason or "",
        retrieval_sources=body.retrievalSources or "",
        trace_id=trace_id,
        sources=body.sources,
        tenant_id=tenant_id,
    )
    try:
        from app.config import settings
        from app.core.llm_user_observer import emit
        emit(
            "feedback.submitted",
            {"feedback": body.feedback,
             "reason": body.reason if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
             "query": body.query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
             "answer": body.answer if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
             "request": {
                         "query": body.query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
                         "answer": body.answer if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
                         "feedback": body.feedback,
                         "conversationId": body.conversationId,
                         "reason": body.reason if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else ""}},
            username=user.username, tenant=user.tenant_id or "default",
            conversation_id=body.conversationId or "",
        )
    except Exception:
        pass
    # like 时写入高频问答对到 Redis（永不过期，后续流程可复用）
    if body.feedback == "like" and body.query:
        try:
            from app.services.feedback_optimizer_service import write_hotqa
            _bg_tasks.add(asyncio.create_task(write_hotqa(
                body.query, body.answer or "", body.retrievalSources or "",
                getattr(user, "tenant_id", None) or "default")))
        except Exception:
            pass
    # dislike 时异步失效缓存 + 自动黑名单判定（累计≥阈值则进黑名单，打通自动链路）
    if body.feedback == "dislike" and body.query:
        try:
            from app.services.feedback_optimizer_service import (
                invalidate_cache_on_dislike, maybe_blacklist_on_dislike, check_overconfident,
            )
            _bg_tasks.add(asyncio.create_task(invalidate_cache_on_dislike(body.query)))
            # maybe 内部用独立 session，后台 task 安全
            _bg_tasks.add(asyncio.create_task(maybe_blacklist_on_dislike(body.query)))
            # T8（断点 G）：dislike×历史high置信 → over_confident 冲突检测 + evidence_gap 复核
            _bg_tasks.add(asyncio.create_task(check_overconfident(
                body.query, getattr(user, "tenant_id", None) or "default")))
        except Exception:
            pass
        # B1 数据飞轮：dislike → 质量事件总线 → 订阅者(evidence_gap 补全等)；opt-in
        from app.config import settings as _cfg
        if getattr(_cfg, "DISLIKE_TO_GAP_ENABLE", False):
            try:
                from app.services.quality_event_bus import emit as _qemit
                _bg_tasks.add(asyncio.create_task(_qemit(
                    "feedback", "dislike",
                    {"query": body.query, "answer": (body.answer or "")[:500]},
                    tenant=getattr(user, "tenant_id", None) or "default",
                )))
            except Exception:
                pass
    return success(None, "感谢反馈")


@router.post("/evidence-gap/report")
@limiter.limit("30/minute")
async def evidence_gap_report(
    request: Request,
    body: dict,
    user: User = Depends(get_current_user),
):
    """用户主动上报证据不足（Chat 对 medium/refused 答案触发）。"""
    from app.services.evidence_gap_service import collect
    gid = await collect(
        term_service.normalize(body.get("query", "")),
        body.get("answer", ""), body.get("confidence", "medium"),
        body.get("grade", ""), body.get("action", ""), "manual", user.tenant_id,
    )
    return success({"id": gid}, "已上报" if gid else "已记录（去重）")


@router.post("/faithfulness")
@limiter.limit("30/minute")
async def faithfulness(
    request: Request,
    body: FaithfulnessRequest,
    user: User = Depends(get_current_user),
):
    """真 faithfulness：LLM-judge 判定答案被引用资料支撑的比例（替代粗糙启发式）。

    流式 done 先下发启发式快值，前端异步拉取本接口覆盖展示（不拖慢首字）。
    """
    from app.rag import judge

    sources = [s.get("text", "") if isinstance(s, dict) else str(s) for s in body.sources]
    res = await judge.judge_hallucination(body.answer, sources, body.modelType)
    return success(res, "评估完成")


@router.get("/feedbacks")
async def list_feedbacks(
    feedback: str = "",
    page: int = 1,
    size: int = 20,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(FEEDBACK_READ)),
):
    """反馈管理台：分页列出 👍/👎（可过滤 dislike 坏 case，租户域内）。"""
    data = await feedback_service.list_feedbacks(
        db, feedback, page, size,
        tenant_id=getattr(user, "tenant_id", None) or "default",
    )
    return success(data, "查询成功")


@router.get("/feedbacks/{feedback_id}")
async def get_feedback(
    feedback_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(FEEDBACK_READ)),
):
    """反馈详情（租户域内；跨租户主键不可见）。"""
    data = await feedback_service.get_feedback(
        db, feedback_id,
        tenant_id=getattr(user, "tenant_id", None) or "default",
    )
    if data is None:
        from app.core.response import BizError
        raise BizError("反馈不存在", 404)
    return success(data, "查询成功")


@router.post("/evidence-trace")
async def evidence_trace(
    body: FaithfulnessRequest,
    user: User = Depends(get_current_user),
):
    """P4-⑮ 证据溯源：对答案做句级引用标注，返回每句话对应哪些资料。"""
    from app.rag.citation import evidence_trace as _trace
    trace = _trace(body.answer or "")
    return success(trace, "分析完成")


@router.get("/feedback-stats")
async def feedback_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(FEEDBACK_READ)),
):
    """故障趋势看板：反馈分布 + 坏 case 设备聚类 + 高频问题 + 平均幻觉率（租户域内）。"""
    data = await feedback_service.feedback_stats(
        db, tenant_id=getattr(user, "tenant_id", None) or "default",
    )
    return success(data, "查询成功")


@router.post("/feedbacks/{feedback_id}/golden")
async def mark_golden(
    feedback_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(FEEDBACK_MANAGE)),
):
    """一键把坏 case 回流 golden_qa.json，让 CI 评测门禁覆盖它（反馈→评测闭环，租户域内）。"""
    data = await feedback_service.mark_golden(
        db, feedback_id,
        tenant_id=getattr(user, "tenant_id", None) or "default",
    )
    msg = "已加入 golden 集" if data.get("added") else (data.get("reason") or "未加入")
    return success(data, msg)


@router.post("/favorites")
async def add_favorite(body: dict, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """收藏问题（+答案快照）到个人收藏夹。"""
    data = await favorite_service.add_favorite(db, user.id, user.username, body.get("query", ""), body.get("answer", ""))
    return success(data, "已收藏")


@router.get("/favorites")
async def list_favorites(keyword: str = "", db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """列出我的收藏（可按关键词筛）。"""
    return success(await favorite_service.list_favorites(db, user.id, keyword), "查询成功")


@router.delete("/favorites/{favorite_id}")
async def delete_favorite(favorite_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """删除一条收藏（仅本人）。"""
    data = await favorite_service.delete_favorite(db, favorite_id, user.id)
    return success(data, "已删除")


@router.websocket("/answer/ws")
async def answer_ws(ws: WebSocket):
    """WebSocket 流式问答（双向，SSE 的增强版，为服务端主动推送留能力）。

    鉴权：?token=JWT；客户端 send_json({query, modelType, conversationId})；
    服务端逐条 send_json(stream_answer 的 meta/token/done 事件)。
    """
    from app.core.security import decode_token
    from app.db.session import AsyncSessionLocal
    from app.services.auth_service import get_user_by_id

    token = ws.query_params.get("token", "")
    try:
        payload = decode_token(token)
        user_id = payload.get("sub", "")
    except Exception:
        await ws.accept()
        await ws.send_json({"type": "error", "message": "未认证或 token 无效"})
        await ws.close()
        return

    await ws.accept()
    observer_user = None
    observer_query = ""
    observer_conversation = ""
    observer_trace = ""
    terminal_emitted = False
    try:
        req = await ws.receive_json()
        query = (req.get("query") or "").strip()
        if not query:
            await ws.send_json({"type": "error", "message": "query 为空"})
            await ws.close()
            return
        async with AsyncSessionLocal() as db:
            user = await get_user_by_id(db, user_id)
            if not user:
                await ws.send_json({"type": "error", "message": "用户不存在"})
                await ws.close()
                return
            from app.config import settings
            from app.core.qa_trace import new_collector
            from app.core.llm_user_observer import emit
            _trace_c = new_collector(query) if getattr(settings, "QA_TRACE_ENABLE", True) else None
            observer_user = user
            observer_query = query
            observer_conversation = req.get("conversationId") or ""
            observer_trace = _trace_c.trace_id if _trace_c else ""
            emit(
                "qa.websocket.started",
                {
                    "query": query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
                    "modelType": req.get("modelType") or "",
                    "method": "GET", "path": "/api/qa/ws",
                    "request": {
                        "query": query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
                        "modelType": req.get("modelType"), "conversationId": req.get("conversationId"),
                    },
                },
                username=user.username, tenant=user.tenant_id or "default",
                conversation_id=observer_conversation, qa_trace_id=observer_trace,
            )
            if observer_conversation:
                emit(
                    "conversation.continued", {"channel": "websocket"},
                    username=user.username, tenant=user.tenant_id or "default",
                    conversation_id=observer_conversation, qa_trace_id=observer_trace,
                )
            token_count = 0
            answer_parts: list[str] = []
            answer_chars = 0
            first_token_ms = None
            stream_started_at = time.perf_counter()
            conversation_started_emitted = bool(observer_conversation)
            async for item in qa_service.stream_answer(
                db, query, req.get("modelType"),
                conversation_id=req.get("conversationId"),
                username=user.username, tenant=user.tenant_id,
                # 与 SSE 端点对齐：agent 工具角色门禁与文档级 dept/role ACL 依赖这两个字段
                user_dept=user.dept, user_role=user.role,
                agent_mode=bool(req.get("agentMode")),
                memory_read=bool(req.get("memoryRead")),
                memory_write=bool(req.get("memoryWrite")),
                memory_scope=str(req.get("memoryScope") or "user"),
                trace_id=observer_trace or "",
            ):
                if isinstance(item, dict) and item.get("type") == "token":
                    token_count += 1
                    if settings.LLM_USER_OBSERVER_CAPTURE_TEXT and answer_chars < 16000:
                        content = str(item.get("content", ""))[:16000 - answer_chars]
                        answer_parts.append(content)
                        answer_chars += len(content)
                    if first_token_ms is None:
                        first_token_ms = round((time.perf_counter() - stream_started_at) * 1000, 3)
                        emit(
                            "qa.websocket.first_token", {"firstTokenMs": first_token_ms},
                            username=user.username, tenant=user.tenant_id or "default",
                            conversation_id=observer_conversation, qa_trace_id=observer_trace,
                        )
                if isinstance(item, dict) and item.get("conversationId"):
                    observer_conversation = item["conversationId"]
                    if not conversation_started_emitted:
                        emit(
                            "conversation.started", {"channel": "websocket"},
                            username=user.username, tenant=user.tenant_id or "default",
                            conversation_id=observer_conversation, qa_trace_id=observer_trace,
                        )
                        conversation_started_emitted = True
                if isinstance(item, dict) and item.get("type") == "error" and not terminal_emitted:
                    emit(
                        "qa.websocket.error",
                        {
                            "query": query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
                            "errorType": str(item.get("errorType") or item.get("code") or "stream_error"),
                            "tokenEvents": token_count,
                        },
                        username=user.username, tenant=user.tenant_id or "default",
                        conversation_id=observer_conversation, qa_trace_id=observer_trace,
                    )
                    terminal_emitted = True
                if isinstance(item, dict) and item.get("type") == "done" and not terminal_emitted:
                    if _trace_c is not None:
                        try:
                            item["trace"] = _trace_c.to_dict()
                        except Exception:
                            pass
                    emit(
                        "qa.websocket.completed",
                        {
                            "query": query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
                            "answer": (
                                item.get("content") or item.get("annotatedAnswer") or "".join(answer_parts)
                            ) if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "",
                            "tokenEvents": token_count, "firstTokenMs": first_token_ms,
                            "modelType": item.get("modelType", req.get("modelType") or ""),
                            "route": item.get("route", ""), "confidence": item.get("confidence", ""),
                            "degraded": bool(item.get("llmDegraded") or item.get("retrievalDegraded")),
                            "degradationReason": item.get("llmDegradedReason", item.get("retrievalDegradedReason", "")),
                        },
                        username=user.username, tenant=user.tenant_id or "default",
                        conversation_id=observer_conversation, qa_trace_id=observer_trace,
                    )
                    terminal_emitted = True
                await ws.send_json(item)
    except WebSocketDisconnect:
        if observer_user and not terminal_emitted:
            from app.core.llm_user_observer import emit
            emit(
                "qa.websocket.aborted", {"query": observer_query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else ""},
                username=observer_user.username, tenant=observer_user.tenant_id or "default",
                conversation_id=observer_conversation, qa_trace_id=observer_trace,
            )
            terminal_emitted = True
        return
    except Exception as e:
        if observer_user:
            from app.core.llm_user_observer import emit
            emit(
                "qa.websocket.error", {"query": observer_query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else "", "errorType": type(e).__name__},
                username=observer_user.username, tenant=observer_user.tenant_id or "default",
                conversation_id=observer_conversation, qa_trace_id=observer_trace,
            )
            terminal_emitted = True
        try:
            await ws.send_json({"type": "error", "message": f"{type(e).__name__}: {e}"[:200]})
        except Exception:
            pass
    finally:
        if observer_user and not terminal_emitted:
            from app.config import settings
            from app.core.llm_user_observer import emit
            emit(
                "qa.websocket.aborted",
                {"query": observer_query if settings.LLM_USER_OBSERVER_CAPTURE_TEXT else ""},
                username=observer_user.username, tenant=observer_user.tenant_id or "default",
                conversation_id=observer_conversation, qa_trace_id=observer_trace,
            )
        try:
            await ws.close()
        except Exception:
            pass


@router.post("/related")
@limiter.limit("20/minute")
async def related(
    request: Request,
    body: RelatedRequest,
    user: User = Depends(get_current_user),
):
    """智能推荐：基于当前问答生成 3 个相关追问问题（独立接口，不拖慢流式）。"""
    questions = await qa_service.generate_related(body.query, body.answer, body.modelType)
    return success({"questions": questions}, "生成成功")


@router.post("/export")
@limiter.limit("20/minute")
async def export_doc(
    request: Request,
    body: ExportRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """答案导出 Word：问答 → .docx 运维报告（现场打印归档）。"""
    from fastapi.responses import Response

    from app.services import export_service

    data = export_service.build_docx(body.query, body.answer, body.sources, body.meta)
    await write_log(db, user.username, "导出报告", f"问题：{(body.query or '')[:40]}")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="grid-qa-report.docx"'},
    )


@router.post("/export-xlsx")
@limiter.limit("20/minute")
async def export_xlsx(
    request: Request,
    body: ExportRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """答案导出 Excel：问答 → .xlsx 结构化表格（台账登记/二次处理）。"""
    from fastapi.responses import Response

    from app.services import export_service

    data = export_service.build_xlsx(body.query, body.answer, body.sources, body.meta)
    await write_log(db, user.username, "导出Excel", f"问题：{(body.query or '')[:40]}")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.sheet",
        headers={"Content-Disposition": 'attachment; filename="grid-qa-report.xlsx"'},
    )
