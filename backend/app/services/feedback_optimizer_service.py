"""反馈驱动优化闭环：分析→建议→自动调优。

扩展现有 feedback_service 的被动收集为主动优化回路：
- bad case 模式发现 → 自动生成优化建议
- 缓存自动失效 → dislike 命中缓存的答案时自动清理（Redis L1 + MySQL L2）
- 热点问题 Cache TTL 自动调优 → 高频坏答案进黑名单禁缓存 / 高频好答案延长 TTL
- 知识盲区自动通知 → 连续 N 次 dislike 同一设备 → 生成上报建议

注：查询改写效果追踪由 CRAG 指标(grid_crag_action_total{action=rewritten/refused})覆盖，
不再单独维护 track_rewrite_effectiveness（原函数零调用且需额外检索成本算 recall A/B，ROI 低）。
"""
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.obs import degraded
from app.models.feedback import Feedback
from app.models.qa_cache import QaCache

_OPTIMIZER_REPORT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "optimization_report.json"


# ========== 1. 缓存自动失效 ==========

async def invalidate_cache_on_dislike(query: str) -> int:
    """dislike 命中时，失效该问题的全部缓存（Redis L1 + MySQL L2）。

    底层逻辑：缓存 key = qa:{model}:{normalized_query}，MySQL 用 query_hash(MD5) 精确匹配。
    故按归一化后的 query 精确失效——既补上 Redis L1（旧版只软删 MySQL，坏答案仍在 Redis 继续命中），
    又改掉旧版 `query.like(prefix%)` 前缀匹配的误删/漏删（raw query 与缓存存的 normalized_query 不对齐）。
    用独立 AsyncSession（不共享请求 db）——供后台 task 安全调用，避免请求结束 close session 时
    bg task 仍持有 session 触发 IllegalStateChangeError。
    返回失效条数（MySQL 软删 + Redis 物理删）。
    """
    try:
        from app.services.term_service import normalize as _normalize
        from app.clients import redis_client
        from app.db.session import AsyncSessionLocal
        nq = _normalize(query or "")
        if not nq:
            return 0

        # MySQL L2：按归一化 query 精确软删（覆盖所有 model_type），独立 session
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(QaCache).where(
                    QaCache.is_deleted == 0,
                    QaCache.query_normalized == nq,
                ).limit(50)
            )).scalars().all()
            mysql_n = 0
            for r in rows:
                r.is_deleted = 1
                mysql_n += 1
            if mysql_n:
                await db.commit()

        # Redis L1：SCAN 匹配 qa:*:{nq} 物理删（覆盖所有 model_type）
        redis_n = 0
        try:
            r = redis_client.get_redis()
            async for key in r.scan_iter(match=f"qa:*:{nq}", count=200):
                await r.delete(key)
                redis_n += 1
        except Exception as e:
            degraded("optimizer_cache_invalidate_redis", e)

        return mysql_n + redis_n
    except Exception as e:
        degraded("optimizer_cache_invalidate", e)
        return 0


# ========== 2. 优化建议生成 ==========

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

    # 3c. 缓存命中率建议（基于进程内真实命中率，非硬编码）
    # 底层逻辑：prometheus Counter 进程内不可读，metrics.cache_hit_inc() 维护了
    # 进程内分层 mirror，这里读真实命中率——样本足够(≥10)且低于阈值(20%)才建议。
    cache_rate = 0.0
    cache_snap: dict = {}
    try:
        from app.core import metrics
        cache_rate = metrics.cache_hit_rate()
        cache_snap = metrics.cache_hit_snapshot()
        total_req = sum(cache_snap.values())
        if total_req >= settings.OPTIMIZER_MIN_SAMPLE and cache_rate < settings.OPTIMIZER_CACHE_HIT_FLOOR:
            suggestions.append({
                "type": "cache",
                "severity": "high" if cache_rate < 0.10 else "medium",
                "title": f"缓存命中率偏低（{cache_rate * 100:.0f}%）",
                "detail": f"近 {total_req} 次问答中缓存命中仅 {cache_rate * 100:.0f}%（分层 {cache_snap}），大量请求直达 LLM 浪费成本",
                "actions": [
                    "1) 确认 cache_warmup 已用 golden_qa.json 预热高频问题",
                    "2) 检查 Redis 容量是否需扩容（LRU 淘汰过快会压低命中）",
                    "3) 考虑开启 Semantic Cache 提升模糊匹配命中率",
                ],
            })
    except Exception as e:
        degraded("optimizer_cache_rate", e)

    # 3d. 趋势判断：本周 vs 上周 dislike 环比（比单一近期占比更能反映"上升"）
    now = datetime.now()
    this_week_start = now - timedelta(days=7)
    last_week_start = now - timedelta(days=14)
    recent_dislike = (await db.execute(
        select(func.count()).where(
            Feedback.feedback == "dislike",
            Feedback.created_at >= this_week_start,
        )
    )).scalar() or 0
    last_week_dislike = (await db.execute(
        select(func.count()).where(
            Feedback.feedback == "dislike",
            Feedback.created_at >= last_week_start,
            Feedback.created_at < this_week_start,
        )
    )).scalar() or 0
    total_dislike = (await db.execute(
        select(func.count()).where(Feedback.feedback == "dislike")
    )).scalar() or 0
    if recent_dislike > 0:
        if last_week_dislike == 0:
            suggestions.append({
                "type": "trend",
                "severity": "high",
                "title": "本周新增失分",
                "detail": f"近7天新增 {recent_dislike} 次 dislike（上周 0 次），疑似新设备上线或文档变更引入",
                "actions": ["排查近 7 天文档变更是否影响检索", "检查是否有新设备类型未覆盖"],
            })
        else:
            wow = recent_dislike / last_week_dislike
            if wow >= settings.OPTIMIZER_TREND_RATIO:
                suggestions.append({
                    "type": "trend",
                    "severity": "high",
                    "title": f"失分环比上升（×{wow:.1f}）",
                    "detail": f"本周 dislike {recent_dislike} 次 vs 上周 {last_week_dislike} 次，环比上升 {((wow - 1) * 100):.0f}%",
                    "actions": ["检查近期文档变更是否降低检索质量", "检查新设备类型覆盖情况"],
                })

    # 3e. LLM 编造检测：检索差但回答"看着好"(judge_halluc 低) = 疑似脱离证据编造（最危险类）
    try:
        fudge_rows = (await db.execute(
            select(Feedback.query).where(
                Feedback.feedback == "dislike",
                Feedback.retrieval_quality.in_(["poor", "partial"]),
                Feedback.judge_halluc < 0.3,
            ).order_by(Feedback.created_at.desc()).limit(10)
        )).scalars().all()
        if fudge_rows:
            suggestions.append({
                "type": "hallucination",
                "severity": "high",
                "title": f"疑似 LLM 编造（{len(fudge_rows)} 例）",
                "detail": "以下问题检索证据不足但回答自信度高，存在 LLM 脱离检索结果编造答案的风险（高危，最该优先处理）",
                "actions": [f"「{(q or '')[:50]}」→ 补充相关文档，或强化 prompt 要求必须引用检索证据" for q in fudge_rows[:5]],
            })
    except Exception as e:
        degraded("optimizer_hallucination_detect", e)

    report = {
        "generatedAt": datetime.now().isoformat(),
        "totalDislike": total_dislike,
        "recentDislike": recent_dislike,
        "cacheHitRate": cache_rate,
        "cacheStats": cache_snap,
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
    return {"generatedAt": None, "totalDislike": 0, "recentDislike": 0,
            "cacheHitRate": 0.0, "cacheStats": {}, "suggestions": [], "suggestionCount": 0}


# ========== 3. 缓存 TTL 自动调优 + 黑名单 ==========

_BLACKLIST_KEY = "qa:cache:blacklist"


async def is_query_blacklisted(nq: str) -> bool:
    """该归一化 query 是否在缓存黑名单（高频坏答案，禁止缓存命中，强制重走 LLM）。"""
    try:
        from app.clients import redis_client
        return bool(await redis_client.get_redis().sismember(_BLACKLIST_KEY, nq))
    except Exception:
        return False


async def auto_tune_cache_ttl(db: AsyncSession) -> dict:
    """基于反馈模式自动调优缓存策略（兑现原 dead code 的承诺）。

    - 高频坏答案（dislike≥3）→ 归一化 query 写入 Redis 黑名单 set，QA 层 is_query_blacklisted 拦截
    - 高频好答案（like≥5）→ 标为 TTL 延长候选（分析展示）
    """
    from app.services.term_service import normalize as _normalize
    tune_results = {"extended": [], "blacklisted": [], "appliedBlacklist": 0}

    # 高频 dislike → 缓存黑名单（归一化后写 Redis set，与缓存 key 对齐）
    bad_queries = (await db.execute(
        select(Feedback.query, func.count()).where(Feedback.feedback == "dislike")
        .group_by(Feedback.query).order_by(func.count().desc()).limit(20)
    )).all()
    blacklist_raw = [q for q, cnt in bad_queries if cnt >= settings.OPTIMIZER_BLACKLIST_THRESHOLD]
    blacklist_nq = sorted({_normalize(q or "") for q in blacklist_raw if q})
    applied = 0
    if blacklist_nq:
        try:
            from app.clients import redis_client
            await redis_client.get_redis().sadd(_BLACKLIST_KEY, *blacklist_nq)
            applied = len(blacklist_nq)
        except Exception as e:
            degraded("optimizer_blacklist_write", e)

    # 高频 like → TTL 延长候选
    good_queries = (await db.execute(
        select(Feedback.query, func.count()).where(Feedback.feedback == "like")
        .group_by(Feedback.query).order_by(func.count().desc()).limit(20)
    )).all()
    extend = [{"query": (q or "")[:60], "count": cnt} for q, cnt in good_queries if cnt >= 5]

    tune_results["blacklisted"] = [q[:60] for q in blacklist_nq]
    tune_results["extended"] = extend
    tune_results["appliedBlacklist"] = applied
    return tune_results


# ========== 4. 黑名单自动触发 + 手动管理 ==========

async def maybe_blacklist_on_dislike(query: str) -> int:
    """dislike 时自动触发：该 query 累计 dislike≥阈值则自动 SADD 黑名单。

    打通 dislike→黑名单→QA拦截 自动链路（不再依赖管理员手动点调优）。
    用独立 session（不依赖请求 db 生命周期，供后台 task 安全调用）。
    返回累计 dislike 数（≥阈值时已写入；<阈值或异常返回 0）。
    """
    try:
        from app.services.term_service import normalize as _normalize
        nq = _normalize(query or "")
        if not nq:
            return 0
        from app.db.session import AsyncSessionLocal
        from app.clients import redis_client
        async with AsyncSessionLocal() as db:
            cnt = (await db.execute(
                select(func.count()).where(Feedback.feedback == "dislike", Feedback.query == query)
            )).scalar() or 0
        if cnt >= settings.OPTIMIZER_BLACKLIST_THRESHOLD:
            await redis_client.get_redis().sadd(_BLACKLIST_KEY, nq)
            return cnt
        return 0
    except Exception as e:
        degraded("optimizer_auto_blacklist", e)
        return 0


async def add_blacklist(query: str) -> str:
    """管理员手动加入黑名单。返回归一化后的 nq。"""
    from app.services.term_service import normalize as _normalize
    from app.clients import redis_client
    nq = _normalize(query or "")
    if nq:
        await redis_client.get_redis().sadd(_BLACKLIST_KEY, nq)
    return nq


async def remove_blacklist(query: str) -> str:
    """管理员手动移出黑名单。返回归一化后的 nq。"""
    from app.services.term_service import normalize as _normalize
    from app.clients import redis_client
    nq = _normalize(query or "")
    if nq:
        await redis_client.get_redis().srem(_BLACKLIST_KEY, nq)
    return nq


async def list_blacklist() -> list[str]:
    """读取当前黑名单成员（归一化 query 列表）。"""
    try:
        from app.clients import redis_client
        return sorted(await redis_client.get_redis().smembers(_BLACKLIST_KEY))
    except Exception:
        return []