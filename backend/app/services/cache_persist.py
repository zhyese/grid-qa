"""缓存持久化服务：MySQL 二级缓存 + Write-Through 双写 + 后台清理。

查询路径:
  cache_get() → None → Redis miss, 外部调用方继续 MySQL → LLM
  cache_get_mysql() → dict | None  (L2 查询)
  cache_set() → Write-Through: MySQL INSERT ON DUPLICATE KEY UPDATE + Redis SET

后台清理:
  cleanup_loop() 每 6h 扫 qa_cache 删除过期/超 3 天未命中行（兜底 MySQL Event Scheduler）
"""
import asyncio
import json
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import redis_client
from app.config import settings
from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.qa_cache import QaCache


async def cache_get_mysql(
    db: AsyncSession, model_type: str | None, normalized_query: str,
) -> dict | None:
    """L2: MySQL 二级缓存查询。命中后异步回写 Redis + 更新 hit_count。"""
    query_hash = QaCache.build_hash(model_type, normalized_query)
    try:
        row = (
            await db.execute(
                select(QaCache).where(
                    QaCache.query_hash == query_hash,
                    QaCache.expires_at > func.now(),
                    QaCache.is_deleted == 0,
                )
            )
        ).scalar_one_or_none()
    except Exception as e:
        degraded("qa_cache_mysql_get", e)
        return None

    if not row:
        return None

    # 更新热度（不阻塞返回）
    try:
        row.hit_count = (row.hit_count or 0) + 1
        row.last_hit_at = datetime.utcnow()
        await db.commit()
    except Exception as e:
        degraded("qa_cache_mysql_hit_update", e)

    # 异步回写 Redis（不阻塞响应）
    try:
        cache_key = f"qa:{model_type or 'default'}:{normalized_query}"
        data = json.loads(row.answer) if isinstance(row.answer, str) else row.answer
        asyncio.ensure_future(
            redis_client.cache_set_json(cache_key, data, settings.QA_CACHE_TTL)
        )
    except Exception as e:
        degraded("qa_cache_redis_backfill", e)

    try:
        from app.core import metrics
        metrics.CACHE_HIT.labels("mysql").inc()
    except Exception:
        pass

    # 还原完整结果 dict
    if isinstance(row.answer, str):
        try:
            return json.loads(row.answer)
        except Exception:
            return None
    return row.answer


async def cache_set_mysql(
    db: AsyncSession,
    model_type: str | None,
    normalized_query: str,
    original_query: str,
    result: dict,
) -> None:
    """Write-Through: 写入 MySQL 二级缓存（ON DUPLICATE KEY UPDATE）。

    先写 MySQL（持久化保证），成功后外部再写 Redis。
    MySQL 写入失败不阻塞——降级记录后仍可走 Redis 热缓存。
    """
    cache_key = f"qa:{model_type or 'default'}:{normalized_query}"
    query_hash = QaCache.build_hash(model_type, normalized_query)
    answer_json = json.dumps(result, ensure_ascii=False)
    ttl = QaCache.ttl_for_query(normalized_query) if settings.CACHE_TIERED_TTL_ENABLE else settings.QA_CACHE_TTL
    now = datetime.utcnow()
    expires_at = now + timedelta(seconds=ttl)

    try:
        # 先查是否存在（避免 ON DUPLICATE KEY UPDATE 的方言兼容问题）
        existing = (
            await db.execute(
                select(QaCache.id, QaCache.hit_count).where(QaCache.query_hash == query_hash)
            )
        ).one_or_none()

        if existing:
            # UPDATE 已有行
            await db.execute(
                text(
                    """UPDATE qa_cache SET
                       answer=:answer, retrieval_sources=:src, confidence=:conf,
                       hallucination_rate=:hall, hit_count=hit_count+1,
                       ttl_seconds=:ttl, expires_at=:exp, last_hit_at=:hit_at,
                       updated_at=:now
                    WHERE query_hash=:qh"""
                ),
                {
                    "answer": answer_json,
                    "src": json.dumps(result.get("retrievalSource", [])),
                    "conf": result.get("confidence", "high"),
                    "hall": result.get("hallucinationRate", 0.0),
                    "ttl": ttl,
                    "exp": expires_at,
                    "hit_at": now,
                    "now": now,
                    "qh": query_hash,
                },
            )
        else:
            # INSERT 新行
            await db.execute(
                text(
                    """INSERT INTO qa_cache
                       (cache_key, model_type, query_hash, query_normalized, query_original,
                        answer, retrieval_sources, confidence, hallucination_rate,
                        hit_count, ttl_seconds, expires_at, last_hit_at, created_at, updated_at,
                        is_deleted)
                    VALUES
                       (:ck, :mt, :qh, :qn, :qo, :ans, :src, :conf, :hall,
                        :hc, :ttl, :exp, :hit_at, :now, :now, 0)"""
                ),
                {
                    "ck": cache_key,
                    "mt": model_type or "default",
                    "qh": query_hash,
                    "qn": normalized_query,
                    "qo": original_query[:1024],
                    "ans": answer_json,
                    "src": json.dumps(result.get("retrievalSource", [])),
                    "conf": result.get("confidence", "high"),
                    "hall": result.get("hallucinationRate", 0.0),
                    "hc": 1,
                    "ttl": ttl,
                    "exp": expires_at,
                    "hit_at": now,
                    "now": now,
                },
            )
        await db.commit()
        try:
            from app.core import metrics
            metrics.CACHE_MYSQL_ROWS.set(1)  # 触发 gauge 更新
        except Exception:
            pass
    except Exception as e:
        degraded("qa_cache_mysql_set", e)
        try:
            from app.core import metrics
            metrics.CACHE_MYSQL_FAIL.inc()
        except Exception:
            pass


async def cache_cleanup(db: AsyncSession) -> int:
    """清理过期缓存：① expires_at < now ② 3 天未命中 ③ 软删超过 7 天。

    MySQL Event Scheduler 是主力（每天 3:00），此函数兜底（应用层每 6h 调一次）。
    返回删除行数。
    """
    total = 0
    try:
        # ① 已过期的（raw SQL：MySQL DELETE 支持 LIMIT，ORM delete().limit() 不支持）
        r1 = await db.execute(
            text("DELETE FROM qa_cache WHERE expires_at < NOW() LIMIT 5000")
        )
        total += r1.rowcount or 0
        # ② 3 天未命中（冷数据，即使未过期也清理）
        r2 = await db.execute(
            text("DELETE FROM qa_cache WHERE updated_at < NOW() - INTERVAL 3 DAY AND is_deleted = 0 LIMIT 5000")
        )
        total += r2.rowcount or 0
        # ③ 软删超过 7 天的物理删除
        r3 = await db.execute(
            text("DELETE FROM qa_cache WHERE is_deleted = 1 AND updated_at < NOW() - INTERVAL 7 DAY LIMIT 1000")
        )
        total += r3.rowcount or 0
        if total > 0:
            await db.commit()
    except Exception as e:
        degraded("qa_cache_cleanup", e)
    return total


def _cache_invalidate_for_doc(doc_id: str) -> int:
    """文档更新/删除后失效关联缓存（同步，供 document_service 调用）。

    由于缓存 key 不直接关联 doc_id，这里通过 retrieval_sources JSON 字段
    做模糊匹配来失效。MySQL 5.7+ 支持 JSON_SEARCH。
    返回失效行数（软删标记 is_deleted=1）。
    """
    # 这是同步函数，因为 milvus_client 操作在同步线程池执行
    # 实际执行需要通过 AsyncSessionLocal
    return 0  # 占位：异步版本见 cache_invalidate_for_doc_async


async def cache_invalidate_for_doc_async(doc_id: str) -> int:
    """文档更新后失效关联缓存（异步版）。

    检索 sources 中包含该 docId 的缓存行 → 软删（is_deleted=1）。
    """
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text(
                    """UPDATE qa_cache SET is_deleted=1, updated_at=NOW()
                    WHERE is_deleted=0
                    AND JSON_SEARCH(retrieval_sources, 'one', :doc_id, NULL, '$[*].docId') IS NOT NULL
                    LIMIT 1000"""
                ),
                {"doc_id": doc_id},
            )
            await db.commit()
            n = result.rowcount or 0
            if n > 0:
                from app.core.obs import degraded
                degraded("cache_invalidate_doc", Exception(f"doc={doc_id}"), f"失效 {n} 条缓存")
            return n
    except Exception as e:
        degraded("cache_invalidate_doc", e)
        return 0


async def cleanup_loop(interval: int = 21600) -> None:
    """后台清理循环：每 interval 秒（默认 6h）扫 qa_cache 过期数据。

    在 FastAPI lifespan 中作为 background task 启动。
    MySQL Event Scheduler 是主力（每天 3:00），此循环为兜底。
    """
    # 启动后先等 60s，确保 DB 就绪
    await asyncio.sleep(60)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                n = await cache_cleanup(db)
                if n > 0:
                    from app.core.obs import degraded
                    degraded("cache_cleanup", Exception(f"清理 {n} 行"), "定期清理过期缓存")
        except Exception as e:
            degraded("cache_cleanup_loop", e)
        await asyncio.sleep(interval)


async def refresh_cache_metrics() -> None:
    """更新 qa_cache 行数 Gauge 指标（供 Grafana 看板）。"""
    try:
        async with AsyncSessionLocal() as db:
            total = await db.execute(
                select(func.count(QaCache.id)).where(QaCache.is_deleted == 0)
            )
            count = total.scalar() or 0
            try:
                from app.core import metrics
                metrics.CACHE_MYSQL_ROWS.set(count)
            except Exception:
                pass
    except Exception as e:
        degraded("cache_metrics_refresh", e)


async def metrics_loop(interval: int = 600) -> None:
    """后台指标刷新循环：每 10 分钟更新缓存行数 Gauge。"""
    await asyncio.sleep(120)  # 启动后等 2 分钟
    while True:
        await refresh_cache_metrics()
        await asyncio.sleep(interval)
