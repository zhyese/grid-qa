"""问答接口：智能问答(普通/流式/多轮) / 对话历史 / 反馈 / 术语归一化。"""
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limiter import limiter
from app.core.response import success
from app.db.session import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.qa import FeedbackRequest, QaAnswerRequest, TermRequest
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
        async for token in qa_service.stream_answer(
            db, body.query, body.modelType, conversation_id=body.conversationId
        ):
            yield f"data: {json.dumps({'content': token}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/conversations")
async def conversations(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await conversation_service.list_conversations(db, user.username)
    return success(data, "查询成功")


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
async def feedback(
    body: FeedbackRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """问答反馈（👍/👎），沉淀坏 case。"""
    await feedback_service.record_feedback(
        db, conversation_id=body.conversationId or "", query=body.query,
        answer=body.answer, feedback=body.feedback, username=user.username,
    )
    return success(None, "感谢反馈")
