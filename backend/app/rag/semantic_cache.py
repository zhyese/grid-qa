"""语义缓存（Semantic Cache）：基于 query embedding 余弦相似度的缓存命中。

扩展三级缓存中的 Redis L1 层：精确 key 匹配 → embedding 余弦相似度匹配。
命中阈值：cosine >= 0.92 直接复用答案，0.85~0.92 标记"高相似"，<0.85 miss。

优化（相对初版 O(n) Python 逐条余弦）：
- embedding 存 fp16 base64（float32→fp16→bytes→base64），索引 JSON 体积 ~1/4。
- 匹配用 numpy matmul：全量余弦一次 BLAS 搞定（5000 条 50ms→~1ms）。
- set() 用进程内 asyncio.Lock 串行化读-改-写，避免并发覆盖丢条目。

存储结构（v3，与旧 float-list/全局索引隔离，旧索引 TTL 自然过期）：
- 精确缓存：qa:{tenant}:{model}:{query} → {answer, ...}
- 语义索引：qa_semantic:tenant:{tenant}:index:v3 → [{query, emb(b64), cache_key, ts}]
"""
import asyncio
import base64
import time

import numpy as np

from app.clients import redis_client
from app.config import settings
from app.core.obs import degraded
from app.services import embedding_service

_SEMANTIC_PREFIX = "qa_semantic"
_SEMANTIC_TTL = 86400 * 3       # 3 天
_SIMILARITY_HIGH = 0.92          # 直接命中
_SIMILARITY_MEDIUM = 0.85        # 快速验证
_MAX_INDEX_SIZE = 5000           # 语义索引上限

_set_lock = asyncio.Lock()       # 串行化 set 的读-改-写，避免并发覆盖丢条目


def _cache_tenant(tenant_id: str | None) -> str:
    return (tenant_id or "default").strip() or "default"


def _semantic_index_key(tenant_id: str | None) -> str:
    return f"{_SEMANTIC_PREFIX}:tenant:{_cache_tenant(tenant_id)}:index:v3"


def _emb_to_b64(vec: list[float]) -> str:
    """float list → fp16 bytes → base64（存储压缩，体积 ~1/4 float32）。"""
    arr = np.asarray(vec, dtype=np.float16)
    return base64.b64encode(arr.tobytes()).decode("ascii")


def _b64_to_emb(s: str) -> np.ndarray:
    """base64 → fp16 np.array → float32（参与 matmul）。"""
    return np.frombuffer(base64.b64decode(s), dtype=np.float16).astype(np.float32)


async def get_cached_key(
    model_type: str | None, query: str, tenant_id: str | None = "default",
) -> str | None:
    """获取精确缓存 key（兼容现有缓存）。"""
    from app.config import citation_cache_version
    return f"qa:{_cache_tenant(tenant_id)}:{model_type or 'default'}:{query}:{citation_cache_version()}"


async def semantic_cache_get(
    model_type: str | None, query: str, *, tenant_id: str | None = "default",
) -> tuple[dict | None, str, float]:
    """语义缓存查询。

    Returns:
        (cached_data, hit_type, similarity)
        hit_type: "exact" | "semantic_high" | "semantic_medium" | "miss"
    """
    if not getattr(settings, "SEMANTIC_CACHE_ENABLE", False):
        return None, "miss", 0.0

    nq = query.strip()
    if not nq:
        return None, "miss", 0.0

    # 1) 先尝试精确命中（已有 Redis L1）
    exact_key = await get_cached_key(model_type, nq, tenant_id)
    try:
        exact = await redis_client.cache_get_json(exact_key)
    except Exception:
        exact = None
    if exact:
        return exact, "exact", 1.0

    # 2) 语义匹配：计算 query embedding → matmul 全量余弦
    try:
        q_emb = await embedding_service.embed_query(nq)
    except Exception as e:
        degraded("semantic_cache_embed", e)
        return None, "miss", 0.0

    model = model_type or "default"
    try:
        index = await redis_client.cache_get_json(_semantic_index_key(tenant_id)) or []
    except Exception:
        index = []
    if not index:
        return None, "miss", 0.0

    # 抽取 b64 embedding → fp16 矩阵（跳过损坏/无 emb 条目）
    rows: list[tuple[np.ndarray, dict]] = []
    for e in index:
        if e.get("model_type", "default") != model:
            continue
        b = e.get("emb")
        if not b:
            continue
        try:
            rows.append((_b64_to_emb(b), e))
        except Exception:
            continue
    if not rows:
        return None, "miss", 0.0

    M = np.vstack([r[0] for r in rows]).astype(np.float32)   # (N, dim)
    norms = np.linalg.norm(M, axis=1, keepdims=True)
    M = M / (norms + 1e-10)                                   # 行归一化
    q = np.asarray(q_emb, dtype=np.float32)
    q = q / (np.linalg.norm(q) + 1e-10)
    sims = M @ q                                              # (N,) 全量余弦，一次 BLAS

    best_i = int(np.argmax(sims))
    best_sim = float(sims[best_i])
    best_entry = rows[best_i][1]

    if best_sim >= _SIMILARITY_HIGH and best_entry:
        cached_key = best_entry.get("cache_key")
        if cached_key:
            try:
                cached = await redis_client.cache_get_json(cached_key)
                if cached:
                    return cached, "semantic_high", best_sim
            except Exception:
                pass

    if best_sim >= _SIMILARITY_MEDIUM and best_entry:
        # 中相似 → 标记但需要回退到快速验证
        cached_key = best_entry.get("cache_key")
        if cached_key:
            try:
                cached = await redis_client.cache_get_json(cached_key)
                if cached:
                    return cached, "semantic_medium", best_sim
            except Exception:
                pass

    return None, "miss", best_sim


async def semantic_cache_set(
    model_type: str | None, query: str, cache_key: str,
    embedding: list[float] | None = None, *, tenant_id: str | None = "default",
) -> None:
    """将查询及其 embedding 加入语义索引。"""
    if not getattr(settings, "SEMANTIC_CACHE_ENABLE", False):
        return

    nq = query.strip()
    if not nq:
        return

    # 获取/传参 embedding
    if embedding is None:
        try:
            embedding = await embedding_service.embed_query(nq)
        except Exception as e:
            degraded("semantic_cache_index_embed", e)
            return
    emb_b64 = _emb_to_b64(embedding)

    # 读-改-写串行化（进程内锁），避免并发覆盖丢条目
    async with _set_lock:
        try:
            index = await redis_client.cache_get_json(_semantic_index_key(tenant_id)) or []
        except Exception:
            index = []

        # 去重：相同 query 已存在则更新
        updated = False
        for e in index:
            if e.get("query") == nq and e.get("model_type", "default") == (model_type or "default"):
                e["emb"] = emb_b64
                e["cache_key"] = cache_key
                e["tenant_id"] = _cache_tenant(tenant_id)
                e["model_type"] = model_type or "default"
                e["ts"] = time.time()
                updated = True
                break
        if not updated:
            # 超限淘汰最旧
            if len(index) >= _MAX_INDEX_SIZE:
                index.sort(key=lambda e: e.get("ts", 0))
                index = index[-_MAX_INDEX_SIZE + 100:]  # 保留最新的 4900 条
            index.append({
                "query": nq,
                "tenant_id": _cache_tenant(tenant_id),
                "model_type": model_type or "default",
                "emb": emb_b64,
                "cache_key": cache_key,
                "ts": time.time(),
            })

        # 写回
        try:
            await redis_client.cache_set_json(_semantic_index_key(tenant_id), index, _SEMANTIC_TTL)
        except Exception as e:
            degraded("semantic_cache_index_write", e)


async def semantic_cache_invalidate(
    model_type: str | None, query: str, tenant_id: str | None = "default",
) -> None:
    """失效语义缓存中特定 query 的条目。"""
    if not getattr(settings, "SEMANTIC_CACHE_ENABLE", False):
        return
    nq = query.strip()
    if not nq:
        return
    async with _set_lock:
        try:
            index = await redis_client.cache_get_json(_semantic_index_key(tenant_id)) or []
            index = [
                e for e in index
                if not (
                    e.get("query") == nq
                    and e.get("model_type", "default") == (model_type or "default")
                )
            ]
            await redis_client.cache_set_json(_semantic_index_key(tenant_id), index, _SEMANTIC_TTL)
        except Exception:
            pass
