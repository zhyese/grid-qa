"""P3-⑬ 知识库质量分 & 知识盲区诊断。

对知识库按 chunk 维度打分 + 整体质量评估 + 盲区发现。
"""
import json
import re
from collections import Counter
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.obs import degraded
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.feedback import Feedback


async def score_knowledge_quality(db: AsyncSession) -> dict:
    """知识库综合质量评分。"""
    # 1) 文档覆盖度
    doc_count = (await db.execute(
        select(func.count()).select_from(Document)
    )).scalar() or 0
    vectorized = (await db.execute(
        select(func.count()).select_from(Document)
        .where(Document.status == "vectorized")
    )).scalar() or 0
    coverage_rate = round(vectorized / max(doc_count, 1), 3)

    # 2) 分块质量（过短/过长块占比）
    chunks = (await db.execute(select(Chunk.content, Chunk.doc_id))).all()
    total = len(chunks)
    too_short = sum(1 for c, _ in chunks if len((c or "").strip()) < 30)
    too_long = sum(1 for c, _ in chunks if len(c or "") > 2000)
    quality_score = round(1.0 - (too_short + too_long * 0.5) / max(total, 1), 3)

    # 3) 重复检测（相似文本比例）
    dup_count = 0
    seen_hashes = set()
    for c, _ in chunks:
        h = hash((c or "")[:100])
        if h in seen_hashes:
            dup_count += 1
        seen_hashes.add(h)
    dup_rate = round(dup_count / max(total, 1), 3)

    # 4) 覆盖场景
    doc_types = (await db.execute(
        select(Document.doc_type, func.count())
        .group_by(Document.doc_type)
    )).all()
    type_dist = {r[0]: r[1] for r in doc_types}
    has_regulation = "运维手册" in type_dist or "规程" in str(type_dist)
    has_case = "故障案例" in type_dist

    # 5) 盲区分析
    gap_suggestions = []
    dislike_queries = (await db.execute(
        select(Feedback.query).where(Feedback.feedback == "dislike")
        .order_by(Feedback.created_at.desc()).limit(100)
    )).scalars().all()
    uncovered_terms = set()
    try:
        from app.services.term_service import _load_terms
        terms = {v for v in _load_terms().values() if v}
    except Exception:
        terms = set()
    doc_tags = (await db.execute(
        select(Document.equipment_tags).where(
            Document.equipment_tags.isnot(None), Document.equipment_tags != ""
        )
    )).scalars().all()
    covered = set()
    for t in doc_tags:
        for tag in (t or "").split(","):
            covered.add(tag.strip()) if tag.strip() else None
    for q in dislike_queries:
        for t in terms:
            if t in (q or "") and t not in covered:
                uncovered_terms.add(t)
    for t in list(uncovered_terms)[:5]:
        gap_suggestions.append({
            "type": "coverage_gap",
            "term": t,
            "suggestion": f"建议上传【{t}】相关运维规程、故障案例或操作手册",
        })

    return {
        "docCount": doc_count,
        "vectorizedCount": vectorized,
        "chunkCount": total,
        "coverageRate": coverage_rate,
        "qualityScore": quality_score,
        "dupRate": dup_rate,
        "tooShortChunks": too_short,
        "tooLongChunks": too_long,
        "hasRegulation": has_regulation,
        "hasFaultCase": has_case,
        "docTypeDistribution": type_dist,
        "gaps": gap_suggestions,
        "overallGrade": _grade(quality_score, coverage_rate, dup_rate, has_regulation, has_case),
    }


def _grade(quality: float, coverage: float, dup_rate: float, has_reg: bool, has_case: bool) -> str:
    score = quality * 40 + coverage * 30 + (1 - dup_rate) * 15
    if has_reg:
        score += 10
    if has_case:
        score += 5
    if score >= 80:
        return "A"
    elif score >= 60:
        return "B"
    elif score >= 40:
        return "C"
    return "D"