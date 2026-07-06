"""改写事件记录 + 聚合查询（供可视化面板）。

log 用独立 AsyncSessionLocal（bg task 安全——记 dislike invalidate session 并发 500 教训）。
stats/events_page 直读，供 system router 接口。
"""
import random
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.config import settings
from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.rewrite_event import RewriteEvent


async def log(strategy: str, original: str, rewritten: str, improved: bool,
              orig_score: float = 0.0, new_score: float = 0.0,
              cached: bool = False, route: str = "hybrid", tenant: str = "default") -> None:
    """采样写一条改写事件。bg task 调用，独立 session。REWRITE_EVENT_SAMPLE_RATE 控制采样。"""
    if random.random() > settings.REWRITE_EVENT_SAMPLE_RATE:
        return
    try:
        async with AsyncSessionLocal() as db:
            db.add(RewriteEvent(
                strategy=strategy, original_query=(original or "")[:500],
                rewritten_query=(rewritten or "")[:500],
                improved=1 if improved else 0,
                orig_score=float(orig_score or 0), new_score=float(new_score or 0),
                cached=1 if cached else 0, route=route, tenant=tenant,
            ))
            await db.commit()
    except Exception as e:
        degraded("rewrite_event_log", e)


def _period_start(period: str) -> datetime:
    n = datetime.now()
    return n - (timedelta(days=1) if period == "today" else timedelta(days=7))


async def stats(period: str = "today") -> dict:
    """聚合统计：总数/采纳/否决/缓存命中/采纳率/缓存命中率/策略分布。"""
    try:
        async with AsyncSessionLocal() as db:
            start = _period_start(period)
            base = select(func.count()).select_from(RewriteEvent).where(RewriteEvent.ts >= start)
            total = (await db.execute(base)).scalar() or 0
            adopted = (await db.execute(base.where(RewriteEvent.improved == 1))).scalar() or 0
            cached = (await db.execute(base.where(RewriteEvent.cached == 1))).scalar() or 0
            rows = (await db.execute(
                select(RewriteEvent.strategy, RewriteEvent.improved, func.count())
                .where(RewriteEvent.ts >= start)
                .group_by(RewriteEvent.strategy, RewriteEvent.improved)
            )).all()
            by_strategy: dict = {}
            for strat, imp, cnt in rows:
                d = by_strategy.setdefault(strat, {"count": 0, "adopted": 0})
                d["count"] += cnt
                if imp:
                    d["adopted"] += cnt
            return {
                "total": total, "adopted": adopted, "rejected": total - adopted,
                "cacheHit": cached,
                "adoptedRate": round(adopted / total, 3) if total else 0,
                "cacheHitRate": round(cached / total, 3) if total else 0,
                "byStrategy": by_strategy,
            }
    except Exception as e:
        degraded("rewrite_event_stats", e)
        return {"total": 0, "adopted": 0, "rejected": 0, "cacheHit": 0,
                "adoptedRate": 0, "cacheHitRate": 0, "byStrategy": {}}


async def events_page(page: int = 1, size: int = 20,
                      strategy: str | None = None, adopted: bool | None = None) -> dict:
    """明细分页：可按策略/采纳过滤。"""
    try:
        async with AsyncSessionLocal() as db:
            q = select(RewriteEvent).order_by(RewriteEvent.ts.desc())
            cq = select(func.count()).select_from(RewriteEvent)
            if strategy:
                q = q.where(RewriteEvent.strategy == strategy)
                cq = cq.where(RewriteEvent.strategy == strategy)
            if adopted is not None:
                q = q.where(RewriteEvent.improved == (1 if adopted else 0))
                cq = cq.where(RewriteEvent.improved == (1 if adopted else 0))
            total = (await db.execute(cq)).scalar() or 0
            rows = (await db.execute(q.offset((page - 1) * size).limit(size))).scalars().all()
            return {"total": total, "list": [{
                "ts": r.ts.strftime("%Y-%m-%d %H:%M:%S") if r.ts else "",
                "strategy": r.strategy, "original": r.original_query, "rewritten": r.rewritten_query,
                "improved": bool(r.improved), "origScore": r.orig_score, "newScore": r.new_score,
                "cached": bool(r.cached),
            } for r in rows]}
    except Exception as e:
        degraded("rewrite_event_page", e)
        return {"total": 0, "list": []}
