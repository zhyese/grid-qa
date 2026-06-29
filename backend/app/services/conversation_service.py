"""对话管理：创建/列表/历史消息/存消息。"""
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, Message


async def create_conversation(db: AsyncSession, username: str, title: str = "") -> Conversation:
    conv = Conversation(username=username, title=(title or "")[:60])
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def list_conversations(db: AsyncSession, username: str, limit: int = 50) -> list[dict]:
    rows = (
        await db.execute(
            select(Conversation)
            .where(Conversation.username == username)
            .order_by(desc(Conversation.created_at))
            .limit(limit)
        )
    ).scalars().all()
    return [
        {"id": r.id, "title": r.title, "createdAt": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else ""}
        for r in rows
    ]


async def get_messages(db: AsyncSession, conversation_id: str, limit: int = 6) -> list[dict]:
    """取最近 limit 条消息（按时间正序返回，供拼上下文）。"""
    rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [{"role": r.role, "content": r.content} for r in reversed(rows)]


async def save_message(db: AsyncSession, conversation_id: str, role: str, content: str) -> None:
    db.add(Message(conversation_id=conversation_id, role=role, content=content))
    await db.commit()
