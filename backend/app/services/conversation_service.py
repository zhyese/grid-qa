"""对话管理：创建/列表/历史消息/存消息。"""
from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, Message


async def create_conversation(db: AsyncSession, username: str, title: str = "") -> Conversation:
    conv = Conversation(username=username, title=(title or "")[:60])
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def list_conversations(db: AsyncSession, username: str, keyword: str = "", limit: int = 50) -> list[dict]:
    stmt = select(Conversation).where(
        Conversation.username == username,
        Conversation.is_deleted == False,  # noqa: E712  软删过滤
    )
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
            .where(Message.conversation_id == conversation_id, Message.is_deleted == False)  # noqa: E712
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [{"id": r.id, "role": r.role, "content": r.content} for r in reversed(rows)]


async def save_message(db: AsyncSession, conversation_id: str, role: str, content: str) -> None:
    db.add(Message(conversation_id=conversation_id, role=role, content=content))
    await db.commit()


async def delete_conversation(db: AsyncSession, username: str, conversation_id: str) -> bool:
    """软删对话(含其下消息)。仅能删自己的（ownership 校验）。DB 行保留，列表/历史过滤掉。"""
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.username == username,
                Conversation.is_deleted == False,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if not conv:
        return False
    conv.is_deleted = True
    await db.execute(
        update(Message)
        .where(Message.conversation_id == conversation_id, Message.is_deleted == False)  # noqa: E712
        .values(is_deleted=True)
    )
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


_MAX_BATCH = 200  # 单次批量删除上限（防超长 IN 列表）


async def batch_delete_conversations(db: AsyncSession, username: str, ids: list[str]) -> int:
    """批量软删会话（含其下消息）。仅删本人会话；返回实际软删条数。"""
    if not ids:
        return 0
    ids = list(dict.fromkeys(ids))[:_MAX_BATCH]  # 去重 + 截断上限
    # 仅取属于该用户、未删的会话（防 ids 里混入他人会话 id）
    owned = (
        await db.execute(
            select(Conversation.id).where(
                Conversation.id.in_(ids),
                Conversation.username == username,
                Conversation.is_deleted == False,  # noqa: E712
            )
        )
    ).scalars().all()
    if not owned:
        return 0
    res = await db.execute(
        update(Conversation).where(Conversation.id.in_(owned)).values(is_deleted=True)
    )
    # 级联软删这些（本人）会话下的消息
    await db.execute(
        update(Message)
        .where(Message.conversation_id.in_(owned), Message.is_deleted == False)  # noqa: E712
        .values(is_deleted=True)
    )
    await db.commit()
    return res.rowcount or 0


async def batch_delete_messages(db: AsyncSession, username: str, ids: list[str]) -> int:
    """批量软删消息。归属校验：只删属于该用户会话下的消息；返回实际软删条数。"""
    if not ids:
        return 0
    ids = list(dict.fromkeys(ids))[:_MAX_BATCH]
    # 先查该用户所有会话 id 集合，再只软删属于这些会话的消息（他人会话消息不动）
    owned_conv_ids = (
        await db.execute(select(Conversation.id).where(Conversation.username == username))
    ).scalars().all()
    if not owned_conv_ids:
        return 0
    res = await db.execute(
        update(Message)
        .where(
            Message.id.in_(ids),
            Message.conversation_id.in_(owned_conv_ids),
            Message.is_deleted == False,  # noqa: E712
        )
        .values(is_deleted=True)
    )
    await db.commit()
    return res.rowcount or 0
