"""通知中心服务：落库 + WS 定向推送（在线即达，离线靠拉取）。

notify_user 语义：落库恒做（失败 degraded 不阻断业务）；WS 推送尽力而为。
所有接入方（签批/报告/演练/评测）只依赖本模块，业务代码零推送细节。
"""
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.obs import degraded
from app.models.notification import Notification


def _row(n: Notification) -> dict:
    return {"id": n.id, "type": n.type, "title": n.title, "body": n.body,
            "link": n.link, "read": bool(n.read_at),
            "createdAt": n.created_at.isoformat() if n.created_at else None}


async def notify_user(username: str, type_: str, title: str, body: str = "",
                      link: str = "", tenant: str = "default") -> str | None:
    """落库 + 在线推送。业务调用点均在服务层（无请求 session），独立开 session。"""
    from app.db.session import AsyncSessionLocal

    if not username:
        return None
    nid = None
    try:
        async with AsyncSessionLocal() as db:
            n = Notification(recipient=username, type=type_,
                             title=(title or "")[:200], body=(body or "")[:1000],
                             link=(link or "")[:255], tenant=tenant)
            db.add(n)
            await db.commit()
            await db.refresh(n)  # MySQL 无 RETURNING，server_default 列需 refresh
            nid = n.id
            row = _row(n)
    except Exception as e:  # noqa: BLE001 — 通知失败不阻断主业务
        degraded("notification_persist", e, f"{type_}->{username}")
        return None
    # WS 在线推送（离线=0 属正常，落库兜底）
    try:
        from app.core import ws_manager
        await ws_manager.send_to_user(username, {"type": "notification", "data": row})
    except Exception as e:  # noqa: BLE001
        degraded("notification_push", e, username)
    return nid


async def notify_many(usernames: list[str], type_: str, title: str, body: str = "",
                      link: str = "", tenant: str = "default") -> int:
    sent = 0
    for u in dict.fromkeys(usernames):  # 去重保序
        if await notify_user(u, type_, title, body, link, tenant):
            sent += 1
    return sent


async def notify_role(db: AsyncSession, roles: list[str], type_: str, title: str,
                      body: str = "", link: str = "", tenant: str = "default") -> int:
    """通知某角色集合的全部 active 用户（如 cron 周报完成 → admin+editor）。"""
    from app.models.user import User

    rows = (await db.execute(
        select(User.username).where(User.tenant_id == tenant,
                                    User.status == "active",
                                    User.role.in_(roles)))).scalars().all()
    return await notify_many(list(rows), type_, title, body, link, tenant)


async def list_notifications(db: AsyncSession, username: str, tenant: str,
                             unread_only: bool = False, limit: int = 50) -> list[dict]:
    conds = [Notification.recipient == username, Notification.tenant == tenant]
    if unread_only:
        conds.append(Notification.read_at.is_(None))
    rows = (await db.execute(
        select(Notification).where(*conds)
        .order_by(Notification.created_at.desc()).limit(min(limit, 200)))).scalars().all()
    return [_row(n) for n in rows]


async def unread_count(db: AsyncSession, username: str, tenant: str) -> int:
    return (await db.execute(
        select(func.count(Notification.id)).where(
            Notification.recipient == username,
            Notification.tenant == tenant,
            Notification.read_at.is_(None)))).scalar() or 0


async def mark_read(db: AsyncSession, notification_id: str, username: str,
                    tenant: str) -> dict:
    n = (await db.execute(select(Notification).where(
        Notification.id == notification_id,
        Notification.recipient == username,
        Notification.tenant == tenant))).scalar_one_or_none()
    if n is None:
        from app.core.response import BizError
        raise BizError("通知不存在", 404)
    if not n.read_at:
        n.read_at = datetime.now()
        await db.commit()
    return {"id": notification_id}


async def mark_all_read(db: AsyncSession, username: str, tenant: str) -> dict:
    rows = (await db.execute(
        select(Notification).where(Notification.recipient == username,
                                   Notification.tenant == tenant,
                                   Notification.read_at.is_(None)))).scalars().all()
    now = datetime.now()
    for n in rows:
        n.read_at = now
    await db.commit()
    return {"marked": len(rows)}
