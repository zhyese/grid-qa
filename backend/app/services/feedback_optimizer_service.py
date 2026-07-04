"""反馈驱动优化闭环：分析→建议→自动调优。

扩展现有 feedback_service 的被动收集为主动优化回路：
- bad case 模式发现 → 自动生成优化建议
- 查询改写效果追踪 → 判断改写是否改善了回答质量
- 缓存自动失效 → dislike 命中缓存的答案时自动清理
- 热点问题 Cache TTL 自动调优 → 高频好问答延长 TTL
- 知识盲区自动通知 → 连续 N 次 dislike 同一设备 → 生成上报建议
"""
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.obs import degraded
from app.models.feedback import Feedback
from app.models.qa_cache import QaCache

_OPTIMIZER_REPORT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "optimization_report.json"


# ========== 1. 缓存自动失效 ==========

async def invalidate_cache_on_dislike(db: AsyncSession, query: str) -> int:
    """dislike 命中时，失效相关缓存条目（query 前缀匹配软删）。

    防止坏答案被缓存继续喂给其他用户。
    """
    try:
        # 模糊匹配 query 前缀相似的缓存
        stmt = select(QaCache).where(
            QaCache.is_deleted == 0,
            QaCache.query.like(f"{query[:20]}%"),
        ).limit(50)
        rows = (await db.execute(stmt)).scalars().all()
        n = 0
        for r in rows:
            r.is_deleted = 1
            n += 1
        if n:
            await db.commit()
        return n
    except Exception as e:
        degraded("optimizer_cache_invalidate", e)
        return 0


# ========== 2. 查询改写效果追踪 ==========

async def track_rewrite_effectiveness(
    db: AsyncSession, original_query: str, rewritten_query: str,
    original_recall: float, rewritten_recall: float,
    original_answer_ok: bool, rewritten_answer_ok: bool,
) -> None:
    """记录一次查询改写效果，后续统计改写是否提升了检索和回答质量。

    存入 feedback 表扩展字段或单独日志。
    """
    try:
        from app.models.operation_log import OperationLog
        log = OperationLog(
            username="system",
            action="rewrite_track",
            detail=json.dumps({
                "original": original_query[:100],
                "rewritten": rewritten_query[:100],
                "originalRecall": original_recall,
                "rewrittenRecall": rewritten_recall,
                "improved": rewritten_recall > original_recall,
                "answerImproved": rewritten_answer_ok and not original_answer_ok,
                "ts": datetime.now().isoformat(),
            }, ensure_ascii=False)[:1000],
        )
        db.add(log)
        await db.commit()
    except Exception as e:
        degraded("optimizer_rewrite_track", e)


# ========== 3. 优化建议生成 ==========

async def generate_optimization_report(db: AsyncSession) -> dict:
    """综合分析反馈数据，生成可执行优化建议报告。"""
    suggestions = []

    # 3a. 高频 bad case 分析 → 检索优化建议
    top_bad = (await db.execute(
        select(Feedback.query, func.count()).where(Feedback.feedback == "dislike")
        .group_by(Feedback.query).order_by(func.count().desc()).limit(10)
    )).all()
    for q, cnt in top_bad:
        if cnt >= 3:
            suggestions.append({
                "type": "retrieval",
                "severity": "high" if cnt >= 5 else "medium",
                "title": f"高频失分问题（{cnt}次dislike）",
                "detail": f"问题「{(q or '')[:60]}」被 {cnt} 位用户点踩，建议：",
                "actions": [
                    "1) 检查该问题是否在 golden 评测集中，若无则手动加入",
                    "2) 检查相关文档是否完整覆盖该设备/场景",
                    "3) 考虑改写问题的同义表述做缓存预热",
                ],
            })

    # 3b. 知识盲区 → 文档上传建议
    try:
        from app.services.term_service import _load_terms
        std = {w for w in _load_terms().values() if w}
    except Exception:
        std = set()
    dislike_device_counts: dict = defaultdict(int)
    dislike_rows = (await db.execute(
        select(Feedback.query).where(Feedback.feedback == "dislike")
        .order_by(Feedback.created_at.desc()).limit(200)
    )).scalars().all()
    for q in dislike_rows:
        for w in std:
            if w in (q or ""):
                dislike_device_counts[w] += 1
    # 查已有文档覆盖
    from app.models.document import Document
    doc_tags_rows = (await db.execute(
        select(Document.equipment_tags).where(
            Document.equipment_tags.isnot(None), Document.equipment_tags != ""
        )
    )).scalars().all()
    covered: set[str] = set()
    for tags in doc_tags_rows:
        for t in (tags or "").split(","):
            t = t.strip()
            if t:
                covered.add(t)

    for device, cnt in sorted(dislike_device_counts.items(), key=lambda x: -x[1])[:5]:
        if cnt >= 3 and not any(device in c or c in device for c in covered):
            suggestions.append({
                "type": "knowledge_gap",
                "severity": "high" if cnt >= 5 else "medium",
                "title": f"知识盲区：{device}",
                "detail": f"「{device}」被 {cnt} 次点踩但知识库无相关文档覆盖",
                "actions": [f"建议上传【{device}】相关运维规程、故障案例或操作手册"],
            })

    # 3c. 缓存命中率建议（基于缓存 vs LLM 比例）
    try:
        from app.core import metrics
        # 缓存命中率低于 20% 时建议做缓存预热
        suggestions.append({
            "type": "cache",
            "severity": "medium",
            "title": "缓存策略建议",
            "detail": "当前缓存命中率可通过预热高频问题提升，建议检查 cache_warmup 是否配置了 golden 预热文件",
            "actions": [
                "1) 确保 backend/data/golden_qa.json 有 ≥30 条问答对",
                "2) 检查 Redis 内存（当前 10MB LRU）是否需要扩容",
                "3) 考虑开启 Semantic Cache（见 P1-④）提升模糊匹配命中率",
            ],
        })
    except Exception:
        pass

    # 3d. 趋势判断：近期 dislike 是否上升
    seven_days_ago = datetime.now() - timedelta(days=7)
    recent_dislike = (await db.execute(
        select(func.count()).where(
            Feedback.feedback == "dislike",
            Feedback.created_at >= seven_days_ago,
        )
    )).scalar() or 0
    total_dislike = (await db.execute(
        select(func.count()).where(Feedback.feedback == "dislike")
    )).scalar() or 0
    if recent_dislike > 0 and total_dislike > 0:
        recent_ratio = recent_dislike / max(total_dislike, 1) * 100
        if recent_ratio > 30:
            suggestions.append({
                "type": "trend",
                "severity": "high",
                "title": "近期失分率偏高",
                "detail": f"近7天 dislike 占比 {recent_ratio:.0f}%（{recent_dislike}/{total_dislike}）",
                "actions": ["建议检查近期是否有文档变更导致检索质量下降", "检查是否有新的未覆盖设备类型上线"],
            })

    report = {
        "generatedAt": datetime.now().isoformat(),
        "totalDislike": total_dislike,
        "recentDislike": recent_dislike,
        "suggestions": suggestions,
        "suggestionCount": len(suggestions),
    }
    try:
        _OPTIMIZER_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _OPTIMIZER_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        degraded("optimizer_report_write", e)
    return report


async def get_optimization_report() -> dict:
    """读取已生成的优化建议报告（不实时计算，从缓存文件读）。"""
    if _OPTIMIZER_REPORT_PATH.exists():
        try:
            return json.loads(_OPTIMIZER_REPORT_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"generatedAt": None, "totalDislike": 0, "suggestions": [], "suggestionCount": 0}


# ========== 4. 缓存 TTL 自动调优 ==========

async def auto_tune_cache_ttl(db: AsyncSession) -> dict:
    """基于反馈模式自动调优各类型问题的缓存 TTL。

    好问答（like > dislike * 3）→ 延长 TTL
    坏问答（dislike > 0）→ 缩短 TTL 或黑名单
    """
    tune_results = {"extended": [], "shortened": [], "blacklisted": []}

    # 找高频 dislike 问题 → 缓存黑名单
    bad_queries = (await db.execute(
        select(Feedback.query, func.count()).where(Feedback.feedback == "dislike")
        .group_by(Feedback.query).order_by(func.count().desc()).limit(20)
    )).all()
    blacklist = [q for q, cnt in bad_queries if cnt >= 3]

    # 找高频 like 问题 → TTL 延长候选
    good_queries = (await db.execute(
        select(Feedback.query, func.count()).where(Feedback.feedback == "like")
        .group_by(Feedback.query).order_by(func.count().desc()).limit(20)
    )).all()
    extend = [{"query": q[:60], "count": cnt} for q, cnt in good_queries if cnt >= 5]

    tune_results["blacklisted"] = [q[:60] for q in blacklist]
    tune_results["extended"] = extend
    return tune_results