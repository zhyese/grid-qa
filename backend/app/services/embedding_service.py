"""向量生成服务：批量/单条，委托给配置的 EmbeddingProvider。query 向量走 Redis 缓存。"""
import json

from app.clients import redis_client
from app.config import settings
from app.core import metrics
from app.core.obs import degraded
from app.providers.factory import get_embedding_provider


async def embed_texts(texts: list[str]) -> list[list[float]]:
    import time
    _t0 = time.time()
    vecs = await get_embedding_provider().embed(texts)
    try:
        metrics.EMBED_CALLS.labels(settings.EMB_PROVIDER).inc(len(vecs))
        metrics.EMBED_LATENCY.labels(settings.EMB_PROVIDER).observe(time.time() - _t0)
    except Exception:
        pass
    return vecs


async def embed_query(text: str, provider: str | None = None) -> list[float]:
    """单条 query 向量，带 Redis 缓存（高频/重复问题省 embedding 调用）。

    B3：命中时可选滑动续期（EMBED_CACHE_SLIDE_TTL_ENABLE，默认关）。
    开关关 → 现状（固定 TTL 自然过期）；开关开 → 命中续期，高频 query 保活。
    """
    p = provider or settings.EMB_PROVIDER
    key = f"emb:{p}:{text}"
    r = redis_client.get_redis()
    try:
        cached = await r.get(key)
        if cached:
            # B3：命中续期（高频 query 保活，默认关）
            if getattr(settings, "EMBED_CACHE_SLIDE_TTL_ENABLE", False):
                try:
                    await r.expire(key, settings.EMBED_CACHE_TTL)
                except Exception:
                    pass
            return json.loads(cached)
    except Exception as e:
        degraded("embed_cache_get", e)
    import time
    _t0 = time.time()
    vec = (await get_embedding_provider(p).embed([text]))[0]
    try:
        metrics.EMBED_CALLS.labels(p).inc()
        metrics.EMBED_LATENCY.labels(p).observe(time.time() - _t0)
    except Exception:
        pass
    try:
        await r.set(key, json.dumps(vec), ex=getattr(settings, "EMBED_CACHE_TTL", 3600))
    except Exception as e:
        degraded("embed_cache_set", e)
    return vec
