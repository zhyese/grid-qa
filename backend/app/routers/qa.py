"""问答接口：智能问答(普通/流式) / 术语归一化。"""
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import success
from app.db.session import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.qa import QaAnswerRequest, TermRequest
from app.services import qa_service, term_service
from app.services.log_service import write_log

router = APIRouter(prefix="/qa", tags=["检索与问答"])


@router.post("/answer")
async def answer(
    body: QaAnswerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await qa_service.answer(db, body.query, body.modelType)
    await write_log(db, user.username, "智能问答", f"提问：{body.query[:50]}")
    return success(data, "问答成功")


@router.post("/answer/stream")
async def answer_stream(
    body: QaAnswerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """SSE 流式问答：逐 token 推送，首字延迟 <1s。"""

    async def gen():
        async for token in qa_service.stream_answer(db, body.query, body.modelType):
            yield f"data: {json.dumps({'content': token}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


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
