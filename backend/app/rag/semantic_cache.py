"""语义缓存（Semantic Cache）：基于 query embedding 相似度匹配的缓存命中。

扩展三级缓存中的 Redis L1 层：精确 key 匹配 → embedding 余弦相似度匹配。
命中阈值：cosine > 0.92 直接复用答案，0.85~0.92 标记为"高相似"走快速检索验证。
低于 0.85 走正常 LLM 全链路。

存储结构：
- 精确缓存：qa:{model}:{query} → {answer, ...} （已有）
- 语义索引：qa_semantic:index → [{query, embedding, answer_hash, ttl}] （新增）
"""
import json
import time
from typing import Any

import numpy as np

from app.clients import redis_client
from app.config import settings
from app.core.obs import degraded
from app.services import embedding_service

_SEMANTIC_PREFIX = "qa_semantic"
_SEMANTIC_INDEX_KEY = f"{_SEMANTIC_PREFIX}:index"
_SEMANTIC_TTL = 86400 * 3       # 3 天
_SIMILARITY_HIGH = 0.92          # 直接命中
_SIMILARITY_MEDIUM = 0.85        # 快速验证
_MAX_INDEX_SIZE = 5000           # 语义索引上限


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))


async def get_cached_key(model_type: str | None, query: str) -> str | None:
    """获取精确缓存 key（兼容现有缓存）。"""
    return f"qa:{model_type or 'default'}:{query}"


async def semantic_cache_get(
    model_type: str | None, query: str,
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
    exact_key = await get_cached_key(model_type, nq)
    try:
        exact = await redis_client.cache_get_json(exact_key)
    except Exception:
        exact = None
    if exact:
        return exact, "exact", 1.0

    # 2) 语义匹配：计算 query embedding → 在索引中找相似
    try:
        q_emb = await embedding_service.embed_query(nq)
    except Exception as e:
        degraded("semantic_cache_embed", e)
        return None, "miss", 0.0

    try:
        index = await redis_client.cache_get_json(_SEMANTIC_INDEX_KEY) or []
    except Exception:
        index = []

    if not index:
        return None, "miss", 0.0

    q_np = np.array(q_emb, dtype=np.float32)
    best_sim = 0.0
    best_entry = None

    for entry in index:
        emb = entry.get("embedding")
        if not emb:
            continue
        e_np = np.array(emb, dtype=np.float32)
        sim = _cosine(q_np, e_np)
        if sim > best_sim:
            best_sim = sim
            best_entry = entry

    if best_sim >= _SIMILARITY_HIGH and best_entry:
        # 高相似 → 直接返回缓存
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
    embedding: list[float] | None = None,
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

    # 读取索引
    try:
        index = await redis_client.cache_get_json(_SEMANTIC_INDEX_KEY) or []
    except Exception:
        index = []

    # 去重：相同 query 已存在则更新
    existing = [e for e in index if e.get("query") == nq]
    if existing:
        entry = existing[0]
        entry["embedding"] = embedding
        entry["cache_key"] = cache_key
        entry["ts"] = time.time()
    else:
        # 超限淘汰最旧
        if len(index) >= _MAX_INDEX_SIZE:
            index.sort(key=lambda e: e.get("ts", 0))
            index = index[-_MAX_INDEX_SIZE + 100:]  # 保留最新的 4900 条
        index.append({
            "query": nq,
            "embedding": embedding,
            "cache_key": cache_key,
            "ts": time.time(),
        })

    # 写回
    try:
        await redis_client.cache_set_json(_SEMANTIC_INDEX_KEY, index, _SEMANTIC_TTL)
    except Exception as e:
        degraded("semantic_cache_index_write", e)


async def semantic_cache_invalidate(model_type: str | None, query: str) -> None:
    """失效语义缓存中特定 query 的条目。"""
    if not getattr(settings, "SEMANTIC_CACHE_ENABLE", False):
        return
    nq = query.strip()
    if not nq:
        return
    try:
        index = await redis_client.cache_get_json(_SEMANTIC_INDEX_KEY) or []
        index = [e for e in index if e.get("query") != nq]
        await redis_client.cache_set_json(_SEMANTIC_INDEX_KEY, index, _SEMANTIC_TTL)
    except Exception:
        pass