"""向量生成服务：批量/单条，委托给配置的 EmbeddingProvider。query 向量走 Redis 缓存。"""
import json

from app.clients import redis_client
from app.config import settings
from app.core import metrics
from app.core.obs import degraded
from app.providers.factory import get_embedding_provider


async def embed_texts(
    texts: list[str],
    chunk_ids: list[str] | None = None,
) -> list[list[float]]:
    """批量向量化，委托给配置的 EmbeddingProvider。

    A1/A4（Batch 2）：可选 chunk_ids 实现 chunk 向量复用。
    - 开关 EMBED_CHUNK_CACHE_ENABLE 默认关 → 行为=现状（不查/不写缓存）。
    - 开关开且 chunk_ids 长度 == texts 长度：按 chunk_id 查 Redis
      （chunk_embed:{provider}:{chunk_id}），命中跳过 embed，miss 才 embed + 存。
    - chunk_id 为空串 / chunk_ids=None / 长度不匹配 → 走现状 embed（不缓存，不报错）。
    - 缓存层异常（Redis down / JSON 解析失败）静默降级，不阻塞 embed。
    - 缓存键含 provider（云/bge 向量空间不同，不混）。

    向后兼容：旧 caller embed_texts(texts) / embed_texts(["a","b"]) 零改动。
    """
    import time

    provider = settings.EMB_PROVIDER
    cache_on = bool(getattr(settings, "EMBED_CHUNK_CACHE_ENABLE", False))
    # 严格对齐：长度不匹配 / 空 → 不启用缓存（防御 caller 误传）
    use_cache = (
        cache_on
        and chunk_ids is not None
        and len(chunk_ids) == len(texts)
    )

    # 1. 命中查询：从缓存读取已有向量，记录哪些 idx 需要 embed
    vecs: list[list[float] | None] = [None] * len(texts)
    miss_idx: list[int] = []
    if use_cache:
        r = redis_client.get_redis()
        for i, cid in enumerate(chunk_ids or []):
            if not cid:           # 空 chunk_id → 不查不存（chunk_id 缺失走现状 embed）
                miss_idx.append(i)
                continue
            try:
                cached = await r.get(f"chunk_embed:{provider}:{cid}")
            except Exception as e:
                degraded("chunk_embed_cache_get", e)
                cached = None     # 降级：当作 miss
            if cached is not None:
                try:
                    vecs[i] = json.loads(cached)
                    continue
                except Exception:
                    vecs[i] = None  # JSON 损坏 → 当作 miss
            miss_idx.append(i)
    else:
        miss_idx = list(range(len(texts)))

    # 2. miss 部分调底层 embed
    if miss_idx:
        miss_texts = [texts[i] for i in miss_idx]
        _t0 = time.time()
        new_vecs = await get_embedding_provider(provider).embed(miss_texts)
        try:
            metrics.EMBED_CALLS.labels(provider).inc(len(new_vecs))
            metrics.EMBED_LATENCY.labels(provider).observe(time.time() - _t0)
        except Exception:
            pass
        for j, i in enumerate(miss_idx):
            vecs[i] = new_vecs[j]
        # 3. miss 部分写缓存（仅 use_cache 时；写失败静默降级）
        if use_cache:
            ttl = getattr(settings, "EMBED_CHUNK_CACHE_TTL", 604800)
            for j, i in enumerate(miss_idx):
                cid = (chunk_ids or [""])[i]
                if not cid or vecs[i] is None:
                    continue
                try:
                    await r.set(
                        f"chunk_embed:{provider}:{cid}",
                        json.dumps(vecs[i]),
                        ex=ttl,
                    )
                except Exception as e:
                    degraded("chunk_embed_cache_set", e)

    # 全部命中时 vecs 已填满；texts 空 → vecs 空
    return [v for v in vecs]  # type: ignore[list-item]


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
