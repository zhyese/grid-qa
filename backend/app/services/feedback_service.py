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
from app.models.document import Document
from app.models.feedback import Feedback

_GOLDEN_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "golden_qa.json"
_bg_tasks: set = set()  # 持有后台 task 引用，防 GC


async def record_feedback(
    db: AsyncSession, *, conversation_id: str, query: str,
    answer: str, feedback: str, username: str, reason: str = "",
    retrieval_sources: str = "",  # 检索命中的文档名列表（逗号分隔）
) -> None:
    fb = Feedback(
        conversation_id=conversation_id or "", query=query, answer=answer,
        feedback=feedback, username=username, reason=(reason or "")[:256],
        retrieval_sources=(retrieval_sources or "")[:2000],
    )
    db.add(fb)
    await db.commit()
    try:
        from app.core import metrics
        metrics.FEEDBACK.labels(feedback).inc()
    except Exception:
        pass
    # dislike 自动异步打 judge 分 + 检索质量评估（坏 case 沉淀，不阻塞反馈接口）
    if feedback == "dislike" and getattr(settings, "ONLINE_FAITHFULNESS_ENABLE", False):
        try:
            _t = asyncio.create_task(_judge_bg(fb.id, query, answer, retrieval_sources))
            _bg_tasks.add(_t)
            _t.add_done_callback(_bg_tasks.discard)
        except Exception as e:
            degraded("feedback_judge_dispatch", e)


async def _judge_bg(feedback_id: str, query: str, answer: str, retrieval_sources: str = "") -> None:
    """后台对 dislike 答案跑 LLM-judge + 检索质量评估，回填 judge_supported/judge_halluc/retrieval_quality。"""
    from app.db.session import AsyncSessionLocal
    from app.rag import judge

    judge_res = None
    retrieval_label = None
    try:
        judge_res = await judge.judge_hallucination(answer, [query], settings.LLM_PROVIDER)
        if judge_res:
            try:
                from app.core import metrics
                metrics.HALLUC.observe(judge_res.get("hallucination", 1.0) or 1.0)  # dislike 触发 judge 实测进 HALLUC
            except Exception:
                pass
    except Exception as e:
        degraded("feedback_judge", e)

    # 有检索来源时，额外评估检索质量
    if retrieval_sources:
        try:
            sources_list = [s.strip() for s in retrieval_sources.split(",") if s.strip()]
            if sources_list:
                ctx_res = await judge.judge_context_relevance(query, sources_list, settings.LLM_PROVIDER)
                score = ctx_res.get("relevance_score", 0.0)
                if score >= 0.7:
                    retrieval_label = "good"
                elif score >= 0.4:
                    retrieval_label = "partial"
                else:
                    retrieval_label = "poor"
        except Exception as e:
            degraded("feedback_retrieval_judge", e)

    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(Feedback).where(Feedback.id == feedback_id)
            )).scalar_one_or_none()
            if row:
                if judge_res:
                    row.judge_supported = judge_res.get("supported_ratio")
                    row.judge_halluc = judge_res.get("hallucination")
                if retrieval_label:
                    row.retrieval_quality = retrieval_label
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
                "retrievalQuality": r.retrieval_quality,
                "retrievalSources": (r.retrieval_sources or "")[:500],
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


async def feedback_stats(db: AsyncSession) -> dict:
    """反馈趋势聚合：点赞/点踩分布 + 坏 case 设备聚类 + 高频问题 + 平均幻觉率（反哺知识库优化）。"""
    by_fb = (await db.execute(
        select(Feedback.feedback, func.count()).group_by(Feedback.feedback)
    )).all()
    fb_map = {r[0]: r[1] for r in by_fb}
    total = sum(fb_map.values())

    # 坏 case 按设备聚类（术语表标准词匹配 query）
    dislike_rows = (await db.execute(
        select(Feedback.query).where(Feedback.feedback == "dislike")
        .order_by(Feedback.created_at.desc()).limit(100)
    )).scalars().all()
    try:
        from app.services.term_service import _load_terms
        std = {w for w in _load_terms().values() if w}
    except Exception:
        std = set()
    device_counts: dict = {}
    for q in dislike_rows:
        for w in std:
            if w in (q or ""):
                device_counts[w] = device_counts.get(w, 0) + 1
    top_devices = sorted(device_counts.items(), key=lambda x: -x[1])[:10]

    # 高频坏 case
    top_bad = (await db.execute(
        select(Feedback.query, func.count()).where(Feedback.feedback == "dislike")
        .group_by(Feedback.query).order_by(func.count().desc()).limit(10)
    )).all()
    # 平均幻觉率（dislike 的 judge 分）
    avg_halluc = (await db.execute(
        select(func.avg(Feedback.judge_halluc)).where(Feedback.feedback == "dislike")
    )).scalar()
    # 检索→回答一致性矩阵（2×2：检索好坏 vs 回答好坏）
    cross_rows = (await db.execute(
        select(Feedback.retrieval_quality, Feedback.judge_halluc)
        .where(Feedback.feedback == "dislike")
        .where(Feedback.retrieval_quality.isnot(None))
        .where(Feedback.judge_halluc.isnot(None))
    )).all()
    # 矩阵：{"good_retrieval_good_answer": N, "good_retrieval_bad_answer": N,
    #         "poor_retrieval_good_answer": N, "poor_retrieval_bad_answer": N}
    matrix = {
        "retrieval_good_answer_good": 0,    # ✅ 正常
        "retrieval_good_answer_bad": 0,     # 🔧 生成问题
        "retrieval_poor_answer_good": 0,    # ⚠️ LLM 编造（危险）
        "retrieval_poor_answer_bad": 0,     # ❌ 检索根因
        "retrieval_poor_answer_good_queries": [],  # 编造 case 具体 query
    }
    for rq, hall in cross_rows:
        if rq == "good":
            if hall is not None and hall < 0.3:
                matrix["retrieval_good_answer_good"] += 1
            else:
                matrix["retrieval_good_answer_bad"] += 1
        elif rq in ("poor", "partial"):
            if hall is not None and hall < 0.3:
                matrix["retrieval_poor_answer_good"] += 1
            else:
                matrix["retrieval_poor_answer_bad"] += 1
    # 拉出"检索差但回答好"的具体 query（疑似 LLM 编造）
    if matrix["retrieval_poor_answer_good"] > 0:
        fudge_rows = (await db.execute(
            select(Feedback.query).where(
                Feedback.feedback == "dislike",
                Feedback.retrieval_quality.in_(["poor", "partial"]),
                Feedback.judge_halluc < 0.3,
            ).limit(10)
        )).scalars().all()
        matrix["retrieval_poor_answer_good_queries"] = list(fudge_rows)[:10]

    # 知识盲区：高频 dislike 设备词 × 已上传文档覆盖情况交叉
    coverage_gaps: list[dict] = []
    if top_devices:
        doc_tags_rows = (await db.execute(
            select(Document.doc_name, Document.equipment_tags, Document.doc_type)
            .where(Document.equipment_tags.isnot(None), Document.equipment_tags != "")
        )).all()
        # 所有文档覆盖的设备词集合
        covered: set[str] = set()
        for _, tags, _ in doc_tags_rows:
            for t in (tags or "").split(","):
                t = t.strip()
                if t:
                    covered.add(t)
        for device, cnt in top_devices:
            is_covered = any(device in c or c in device for c in covered)
            coverage_gaps.append({
                "device": device,
                "dislikeCount": cnt,
                "covered": is_covered,
                "suggestion": "" if is_covered else f"建议上传【{device}】相关运维规程或故障案例",
            })

    return {
        "total": total, "like": fb_map.get("like", 0), "dislike": fb_map.get("dislike", 0),
        "dislikeRate": round(fb_map.get("dislike", 0) / total, 3) if total else 0,
        "topDevices": [{"device": d, "count": c} for d, c in top_devices],
        "topBadCases": [{"query": (q or "")[:60], "count": c} for q, c in top_bad],
        "avgHallucination": round(avg_halluc, 3) if avg_halluc is not None else None,
        "consistencyMatrix": matrix,
        "coverageGaps": coverage_gaps,
    }
