"""收藏 / 常用问题库（个人收藏夹）。"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import BizError
from app.models.favorite import Favorite


async def add_favorite(db: AsyncSession, user_id: str, username: str, query: str, answer: str = "") -> dict:
    query = (query or "").strip()
    if not query:
        raise BizError("问题不能为空", 400)
    fav = Favorite(user_id=user_id, username=username, query=query[:512], answer=(answer or "")[:8000])
    db.add(fav)
    await db.commit()
    await db.refresh(fav)   # 加载 server_default(created_at)，避免 commit 后访问触发 async refresh
    return {"id": fav.id, "query": fav.query, "createdAt": fav.created_at.strftime("%Y-%m-%d %H:%M:%S") if fav.created_at else ""}


async def list_favorites(db: AsyncSession, user_id: str, keyword: str = "") -> list[dict]:
    stmt = select(Favorite).where(Favorite.user_id == user_id).order_by(Favorite.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    kw = (keyword or "").strip()
    out = [{"id": r.id, "query": r.query, "answer": (r.answer or "")[:200],
            "createdAt": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""} for r in rows]
    if kw:
        out = [o for o in out if kw in o["query"]]
    return out


async def delete_favorite(db: AsyncSession, favorite_id: str, user_id: str) -> dict:
    from sqlalchemy import delete as _del
    await db.execute(_del(Favorite).where(Favorite.id == favorite_id, Favorite.user_id == user_id))
    await db.commit()
    return {"id": favorite_id, "deleted": True}
