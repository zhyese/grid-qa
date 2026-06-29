"""问答反馈服务。"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feedback import Feedback


async def record_feedback(
    db: AsyncSession, *, conversation_id: str, query: str,
    answer: str, feedback: str, username: str,
) -> None:
    db.add(Feedback(
        conversation_id=conversation_id or "", query=query,
        answer=answer, feedback=feedback, username=username,
    ))
    await db.commit()
