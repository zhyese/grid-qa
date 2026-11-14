"""知识事务提交后的检索/缓存刷新；各存储独立降级，不撤销已提交的业务状态。"""
from app.core.obs import degraded


_BUMP_GENERATION = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local generation = math.max(current + 1, tonumber(ARGV[1]))
redis.call('SET', KEYS[1], generation)
return generation
"""


async def knowledge_changed(doc_id: str, *, tenant_id: str = "default") -> None:
    """仅在 commit 成功后调用。版本失效覆盖尚未引用新增文档的旧答案。"""
    try:
        from app.services import bm25_service
        bm25_service.mark_dirty()
    except Exception as exc:
        degraded("knowledge_changed_bm25", exc, f"doc={doc_id}")

    # L1/L2 的 key/hash 已包含治理代际；即使 Redis 不可用也先失效本进程。
    # 当前版本代际全局共享，因此其他租户会精确缓存冷失效，但不会删除其数据。
    try:
        from app import config
        from app.clients import redis_client
        config.bump_gov_generation_inproc()
        # Redis短暂失败后，本地可能已领先；恢复时必须原子追平，不能只INCR。
        # 若Redis持续不可用直到进程退出，本地失效仍无法跨重启保真。
        generation = await redis_client.get_redis().eval(
            _BUMP_GENERATION, 1, "qa:gov_gen", config._gov_generation,
        )
        config._gov_generation = max(config._gov_generation, int(generation))
    except Exception as exc:
        degraded("knowledge_changed_generation", exc, f"doc={doc_id}")

    # 语义索引直接引用旧 cache_key，不检查版本；需要显式清目标租户索引。
    # 与 semantic_cache_set 共用锁，避免读-改-写把刚清除的索引重新写回来。
    try:
        from app.clients import redis_client
        from app.rag import semantic_cache
        async with semantic_cache._set_lock:
            await redis_client.get_redis().delete(semantic_cache._semantic_index_key(tenant_id))
    except Exception as exc:
        degraded("knowledge_changed_semantic", exc, f"doc={doc_id}")

    try:
        from app.services.cache_persist import cache_invalidate_for_doc_async
        await cache_invalidate_for_doc_async(doc_id)
    except Exception as exc:
        degraded("knowledge_changed_mysql", exc, f"doc={doc_id}")
