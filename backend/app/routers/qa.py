"""问答接口：智能问答(普通/流式/多轮) / 对话历史 / 反馈 / 术语归一化。"""
import asyncio
import json

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limiter import limiter
from app.core.response import success
from app.db.session import get_db
from app.core.permissions import FEEDBACK_MANAGE, FEEDBACK_READ, QA_ANSWER
from app.dependencies import get_current_user, require_admin, require_perm
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
):
    from app.config import settings
    from app.services import plugin_registry
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
            user_dept=user.dept, user_role=user.role,
        )
    # 插件 · 答案后处理（扩展点）
    if isinstance(data, dict) and data.get("answer"):
        data["answer"] = plugin_registry.run_hook("answer_postprocess", data["answer"], {"query": q})
    # X-Cache-Hit 响应头：供 HTTP 层面调试/监控缓存分层命中了哪层
    layer = data.get("cacheLayer") or data.get("cached") and "redis" or "llm"
    request.state.cache_layer = layer
    await write_log(db, user.username, "智能问答", f"提问：{body.query[:50]}")
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
    async def gen():
        async for item in qa_service.stream_answer(
            db, body.query, body.modelType,
            conversation_id=body.conversationId, username=user.username, tenant=user.tenant_id,
            regen=regen, agent_mode=body.agentMode,
            user_dept=user.dept, user_role=user.role,
        ):
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


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
    await feedback_service.record_feedback(
        db, conversation_id=body.conversationId or "", query=body.query,
        answer=body.answer, feedback=body.feedback, username=user.username,
        reason=body.reason or "",
        retrieval_sources=body.retrievalSources or "",
    )
    # dislike 时异步失效缓存 + 自动黑名单判定（累计≥阈值则进黑名单，打通自动链路）
    if body.feedback == "dislike" and body.query:
        try:
            from app.services.feedback_optimizer_service import (
                invalidate_cache_on_dislike, maybe_blacklist_on_dislike,
            )
            _bg_tasks.add(asyncio.create_task(invalidate_cache_on_dislike(body.query)))
            # maybe 内部用独立 session，后台 task 安全
            _bg_tasks.add(asyncio.create_task(maybe_blacklist_on_dislike(body.query)))
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
    """反馈管理台：分页列出 👍/👎（可过滤 dislike 坏 case）。"""
    data = await feedback_service.list_feedbacks(db, feedback, page, size)
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
    """故障趋势看板：反馈分布 + 坏 case 设备聚类 + 高频问题 + 平均幻觉率（反哺优化）。"""
    data = await feedback_service.feedback_stats(db)
    return success(data, "查询成功")


@router.post("/feedbacks/{feedback_id}/golden")
async def mark_golden(
    feedback_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(FEEDBACK_MANAGE)),
):
    """一键把坏 case 回流 golden_qa.json，让 CI 评测门禁覆盖它（反馈→评测闭环）。"""
    data = await feedback_service.mark_golden(db, feedback_id)
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
            async for item in qa_service.stream_answer(
                db, query, req.get("modelType"),
                conversation_id=req.get("conversationId"),
                username=user.username, tenant=user.tenant_id,
            ):
                await ws.send_json(item)
    except WebSocketDisconnect:
        return
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "message": f"{type(e).__name__}: {e}"[:200]})
        except Exception:
            pass
    finally:
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
