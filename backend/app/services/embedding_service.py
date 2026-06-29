"""向量生成服务：批量/单条，委托给配置的 EmbeddingProvider。query 向量走 Redis 缓存。"""
import json

from app.clients import redis_client
from app.config import settings
from app.core import metrics
from app.providers.factory import get_embedding_provider


async def embed_texts(texts: list[str]) -> list[list[float]]:
    vecs = await get_embedding_provider().embed(texts)
    try:
        metrics.EMBED_CALLS.labels(settings.EMB_PROVIDER).inc(len(vecs))
    except Exception:
        pass
    return vecs


async def embed_query(text: str, provider: str | None = None) -> list[float]:
    """单条 query 向量，带 Redis 缓存（高频/重复问题省 embedding 调用）。"""
    p = provider or settings.EMB_PROVIDER
    key = f"emb:{p}:{text}"
    r = redis_client.get_redis()
    try:
        cached = await r.get(key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    vec = (await get_embedding_provider(p).embed([text]))[0]
    try:
        metrics.EMBED_CALLS.labels(p).inc()
    except Exception:
        pass
    try:
        await r.set(key, json.dumps(vec), ex=3600)
    except Exception:
        pass
    return vec
