"""问答缓存的租户隔离契约测试。"""
import pytest

from app.config import settings
from app.models.qa_cache import QaCache
from app.rag import semantic_cache
from app.services import qa_service


def test_exact_cache_key_and_mysql_hash_are_tenant_scoped():
    assert qa_service._cache_key(None, "同一个问题", "tenant-a") == "qa:tenant-a:default:同一个问题"
    assert qa_service._cache_key(None, "同一个问题", "tenant-b") == "qa:tenant-b:default:同一个问题"
    assert qa_service._cache_key("deepseek", "同一个问题", "tenant-a") != qa_service._cache_key(
        "deepseek", "同一个问题", "tenant-b"
    )
    assert QaCache.build_hash("deepseek", "同一个问题", "tenant-a") != QaCache.build_hash(
        "deepseek", "同一个问题", "tenant-b"
    )


@pytest.mark.asyncio
async def test_semantic_cache_index_is_tenant_scoped(monkeypatch):
    store = {}

    async def fake_get(key):
        return store.get(key)

    async def fake_set(key, value, _ttl):
        store[key] = value

    async def fake_embed(_query):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(semantic_cache.redis_client, "cache_get_json", fake_get)
    monkeypatch.setattr(semantic_cache.redis_client, "cache_set_json", fake_set)
    monkeypatch.setattr(semantic_cache.embedding_service, "embed_query", fake_embed)

    await semantic_cache.semantic_cache_set(
        "deepseek", "同一个问题", "qa:tenant-a:deepseek:同一个问题", tenant_id="tenant-a"
    )
    await semantic_cache.semantic_cache_set(
        "deepseek", "同一个问题", "qa:tenant-b:deepseek:同一个问题", tenant_id="tenant-b"
    )

    key_a = "qa_semantic:tenant:tenant-a:index:v3"
    key_b = "qa_semantic:tenant:tenant-b:index:v3"
    assert key_a in store
    assert key_b in store
    assert store[key_a][0]["cache_key"].startswith("qa:tenant-a:")
    assert store[key_b][0]["cache_key"].startswith("qa:tenant-b:")
