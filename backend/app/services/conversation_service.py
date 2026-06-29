"""对话管理：创建/列表/历史消息/存消息。"""
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, Message


async def create_conversation(db: AsyncSession, username: str, title: str = "") -> Conversation:
    conv = Conversation(username=username, title=(title or "")[:60])
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def list_conversations(db: AsyncSession, username: str, keyword: str = "", limit: int = 50) -> list[dict]:
    stmt = select(Conversation).where(Conversation.username == username)
    if keyword:
        stmt = stmt.where(Conversation.title.like(f"%{keyword}%"))
    stmt = stmt.order_by(desc(Conversation.created_at)).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
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


async def delete_conversation(db: AsyncSession, username: str, conversation_id: str) -> bool:
    """删除对话(含消息)。仅能删自己的（ownership 校验）。"""
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.username == username
            )
        )
    ).scalar_one_or_none()
    if not conv:
        return False
    await db.execute(delete(Message).where(Message.conversation_id == conversation_id))
    await db.delete(conv)
    await db.commit()
    return True


async def rename_conversation(db: AsyncSession, username: str, conversation_id: str, title: str) -> bool:
    """重命名对话。仅能改自己的。"""
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.username == username
            )
        )
    ).scalar_one_or_none()
    if not conv:
        return False
    conv.title = (title or "")[:128]
    await db.commit()
    return True
