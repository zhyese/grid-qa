"""问答反馈服务：记录 👍/👎 + 坏 case 自动 judge + 列表管理 + 一键回流 golden 评测集。

闭环：用户 dislike → 异步 LLM-judge 打分 → 管理员在反馈台确认 → 一键标 golden
      → golden_qa.json 增长 → CI 评测门禁越来越硬（坏 case 永久进入回归）。
"""
import asyncio
import json
from pathlib import Path

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.obs import degraded
from app.models.feedback import Feedback

_GOLDEN_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "golden_qa.json"
_bg_tasks: set = set()  # 持有后台 task 引用，防 GC


async def record_feedback(
    db: AsyncSession, *, conversation_id: str, query: str,
    answer: str, feedback: str, username: str, reason: str = "",
) -> None:
    fb = Feedback(
        conversation_id=conversation_id or "", query=query, answer=answer,
        feedback=feedback, username=username, reason=(reason or "")[:256],
    )
    db.add(fb)
    await db.commit()
    try:
        from app.core import metrics
        metrics.FEEDBACK.labels(feedback).inc()
    except Exception:
        pass
    # dislike 自动异步打 judge 分（坏 case 沉淀质量信号，不阻塞反馈接口）
    if feedback == "dislike" and getattr(settings, "ONLINE_FAITHFULNESS_ENABLE", False):
        try:
            _t = asyncio.create_task(_judge_bg(fb.id, query, answer))
            _bg_tasks.add(_t)
            _t.add_done_callback(_bg_tasks.discard)
        except Exception as e:
            degraded("feedback_judge_dispatch", e)


async def _judge_bg(feedback_id: str, query: str, answer: str) -> None:
    """后台对 dislike 答案跑 LLM-judge，回填 judge_supported/judge_halluc。"""
    from app.db.session import AsyncSessionLocal
    from app.rag import judge

    try:
        res = await judge.judge_hallucination(answer, [query], settings.LLM_PROVIDER)
    except Exception as e:
        degraded("feedback_judge", e)
        return
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(Feedback).where(Feedback.id == feedback_id)
            )).scalar_one_or_none()
            if row:
                row.judge_supported = res.get("supported_ratio")
                row.judge_halluc = res.get("hallucination")
                await db.commit()
    except Exception as e:
        degraded("feedback_judge_write", e)


async def list_feedbacks(
    db: AsyncSession, feedback: str = "", page: int = 1, size: int = 20,
) -> dict:
    """反馈列表（管理台用，可按 like/dislike 过滤）。"""
    stmt = select(Feedback)
    cnt = select(func.count()).select_from(Feedback)
    if feedback:
        stmt = stmt.where(Feedback.feedback == feedback)
        cnt = cnt.where(Feedback.feedback == feedback)
    total = (await db.execute(cnt)).scalar() or 0
    rows = (
        await db.execute(
            stmt.order_by(desc(Feedback.created_at)).offset((page - 1) * size).limit(size)
        )
    ).scalars().all()
    return {
        "total": total,
        "list": [
            {
                "id": r.id, "query": r.query, "answer": (r.answer or "")[:300],
                "feedback": r.feedback, "reason": r.reason or "",
                "judgeSupported": r.judge_supported, "judgeHalluc": r.judge_halluc,
                "username": r.username,
                "createdAt": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
            }
            for r in rows
        ],
    }


async def mark_golden(db: AsyncSession, feedback_id: str) -> dict:
    """一键把坏 case 回流到 golden_qa.json（去重），让 CI 门禁覆盖它。"""
    fb = (await db.execute(select(Feedback).where(Feedback.id == feedback_id))).scalar_one_or_none()
    if not fb:
        return {"added": False, "reason": "反馈不存在"}
    try:
        items = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8")) if _GOLDEN_PATH.exists() else []
    except Exception:
        items = []
    if any((it.get("query") or "").strip() == fb.query.strip() for it in items):
        return {"added": False, "total": len(items), "reason": "该问题已在 golden 集"}
    items.append({"query": fb.query.strip(), "expect": [], "category": "用户反馈", "source": "feedback"})
    _GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _GOLDEN_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"added": True, "total": len(items), "query": fb.query.strip()}
