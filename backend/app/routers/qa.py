"""问答接口：智能问答(普通/流式/多轮) / 对话历史 / 反馈 / 术语归一化。"""
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limiter import limiter
from app.core.response import success
from app.db.session import get_db
from app.dependencies import get_current_user, require_admin
from app.models.user import User
from app.schemas.qa import (
    FaithfulnessRequest,
    FeedbackRequest,
    QaAnswerRequest,
    RelatedRequest,
    RenameRequest,
    TermRequest,
)
from app.services import conversation_service, feedback_service, qa_service, term_service
from app.services.log_service import write_log

router = APIRouter(prefix="/qa", tags=["检索与问答"])


@router.post("/answer")
@limiter.limit("30/minute")
async def answer(
    request: Request,
    body: QaAnswerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await qa_service.answer(
        db, body.query, body.modelType, conversation_id=body.conversationId, username=user.username
    )
    await write_log(db, user.username, "智能问答", f"提问：{body.query[:50]}")
    return success(data, "问答成功")


@router.post("/answer/stream")
@limiter.limit("30/minute")
async def answer_stream(
    request: Request,
    body: QaAnswerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    async def gen():
        async for item in qa_service.stream_answer(
            db, body.query, body.modelType,
            conversation_id=body.conversationId, username=user.username,
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
    """问答反馈（👍/👎），沉淀坏 case；dislike 自动异步打 judge 分。"""
    await feedback_service.record_feedback(
        db, conversation_id=body.conversationId or "", query=body.query,
        answer=body.answer, feedback=body.feedback, username=user.username,
        reason=body.reason or "",
    )
    return success(None, "感谢反馈")


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
    admin: User = Depends(require_admin),
):
    """反馈管理台：分页列出 👍/👎（可过滤 dislike 坏 case）。"""
    data = await feedback_service.list_feedbacks(db, feedback, page, size)
    return success(data, "查询成功")


@router.post("/feedbacks/{feedback_id}/golden")
async def mark_golden(
    feedback_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """一键把坏 case 回流 golden_qa.json，让 CI 评测门禁覆盖它（反馈→评测闭环）。"""
    data = await feedback_service.mark_golden(db, feedback_id)
    msg = "已加入 golden 集" if data.get("added") else (data.get("reason") or "未加入")
    return success(data, msg)


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
