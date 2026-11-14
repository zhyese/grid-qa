"""知识回流的提交后检索一致性；外部 Redis/embedding/Milvus 使用边界替身。"""
import asyncio
import copy
import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.clients import redis_client
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.knowledge_evolution import KnowledgeEvolutionDraft
from app.models.qa_cache import QaCache
from app.rag import semantic_cache
from app.services import bm25_service, cache_persist, knowledge_evolution_service as evolution


@pytest.fixture
def cache_environment(monkeypatch):
    import app.config as config

    data = {}
    invalidated = []

    async def get_json(key):
        return copy.deepcopy(data.get(key))

    async def set_json(key, value, ttl):
        data[key] = copy.deepcopy(value)

    async def delete(*keys):
        return sum(data.pop(key, None) is not None for key in keys)

    async def incr(key):
        data[key] = data.get(key, 0) + 1
        return data[key]

    async def atomic_generation(script, key_count, key, minimum):
        # Redis边界替身；同一回归另有独立Redis参数，执行真实Lua验证脚本。
        assert key_count == 1
        data[key] = max(data.get(key, 0) + 1, int(minimum))
        return data[key]

    async def get_value(key):
        return data.get(key)

    async def set_value(key, value):
        data[key] = value

    async def invalidate_doc(doc_id):
        invalidated.append(doc_id)
        return 1

    async def embed(query):
        return [1.0, 0.0]

    monkeypatch.setattr(config, "_gov_generation", 0)
    monkeypatch.setattr(config.settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(config.settings, "SEMANTIC_CACHE_GOV_FILTER_ENABLE", False)
    monkeypatch.setattr(bm25_service, "_dirty", False)
    monkeypatch.setattr(redis_client, "cache_get_json", get_json)
    monkeypatch.setattr(redis_client, "cache_set_json", set_json)
    client = SimpleNamespace(delete=delete, incr=incr, eval=atomic_generation, get=get_value, set=set_value)
    monkeypatch.setattr(redis_client, "get_redis", lambda: client)
    monkeypatch.setattr(cache_persist, "cache_invalidate_for_doc_async", invalidate_doc)
    monkeypatch.setattr(semantic_cache.embedding_service, "embed_query", embed)
    return SimpleNamespace(data=data, invalidated=invalidated, client=client)


async def _seed_cached_answer(db, tenant, query):
    answer = {"answer": "旧知识答案", "retrievalSource": [{"docId": f"ai-evo-{tenant}"}]}
    key = await semantic_cache.get_cached_key(None, query, tenant)
    await redis_client.cache_set_json(key, answer, 600)
    await semantic_cache.semantic_cache_set(None, query, key, [1.0, 0.0], tenant_id=tenant)
    db.add(QaCache(
        tenant_id=tenant, cache_key=key, model_type="", query_hash=QaCache.build_hash(None, query, tenant),
        query_normalized=query, query_original=query, answer=json.dumps(answer),
        retrieval_sources=answer["retrievalSource"], expires_at=QaCache.compute_expires(),
    ))
    await db.commit()
    return key


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reflow", "withdraw"])
async def test_committed_change_invalidates_all_answer_layers(test_db, monkeypatch, cache_environment, operation):
    """删除任一提交后 hook 会留下 BM25、L1、L2 或语义旧答案。"""
    tenant = "north"
    doc_id = "ai-evo-north"
    test_db.add(Document(id=doc_id, doc_name="AI", minio_object="ai", tenant_id=tenant, chunk_count=1))
    test_db.add(Chunk(id="old-chunk", doc_id=doc_id, chunk_idx=0, content="旧草稿"))
    draft = KnowledgeEvolutionDraft(
        id="draft", tenant_id=tenant, status="approved" if operation == "reflow" else "indexed",
        chunk_id="old-chunk", draft_content="新知识", draft_title="新知识",
    )
    test_db.add(draft)
    await test_db.commit()
    await _seed_cached_answer(test_db, tenant, "旧查询")
    other_key = await _seed_cached_answer(test_db, "south", "其他租户")

    async def persist(db, row):
        db.add(Chunk(id="new-chunk", doc_id=doc_id, chunk_idx=1, content=row.draft_content))
        return "new-chunk"

    async def delete_milvus(chunk):
        return None

    monkeypatch.setattr(evolution, "_persist_chunk_to_kb", persist)
    monkeypatch.setattr(evolution, "_delete_chunk_from_milvus", delete_milvus)
    if operation == "reflow":
        await evolution.reflow_to_kb(test_db, draft)
    else:
        await evolution.withdraw_draft(test_db, draft.id, tenant)

    assert bm25_service._dirty is True
    assert cache_environment.invalidated == [doc_id]
    assert await redis_client.cache_get_json(await semantic_cache.get_cached_key(None, "旧查询", tenant)) is None
    assert await cache_persist.cache_get_mysql(test_db, None, "旧查询", tenant) is None
    assert (await semantic_cache.semantic_cache_get(None, "相似查询", tenant_id=tenant))[0] is None
    # 全局版本会让其他租户精确缓存冷失效，但不删除其他租户的实体/语义索引。
    assert cache_environment.data[other_key]["answer"] == "旧知识答案"
    assert cache_environment.data["qa_semantic:tenant:south:index:v3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reflow", "withdraw"])
async def test_failed_commit_leaves_cache_generation_unchanged(test_db, monkeypatch, cache_environment, operation):
    import app.config as config

    draft = KnowledgeEvolutionDraft(id="draft", tenant_id="north", status="approved" if operation == "reflow" else "indexed")
    test_db.add(draft)
    await test_db.commit()

    async def fail_commit():
        raise RuntimeError("commit failed")

    async def persist(db, row):
        return "new-chunk"

    monkeypatch.setattr(test_db, "commit", fail_commit)
    monkeypatch.setattr(evolution, "_persist_chunk_to_kb", persist)
    before = config.citation_cache_version()
    with pytest.raises(RuntimeError, match="commit failed"):
        if operation == "reflow":
            await evolution.reflow_to_kb(test_db, draft)
        else:
            await evolution.withdraw_draft(test_db, draft.id, "north")
    assert config.citation_cache_version() == before
    assert not bm25_service._dirty
    assert not cache_environment.invalidated


@pytest.mark.asyncio
async def test_withdraw_updates_document_chunk_count(test_db, monkeypatch, cache_environment):
    test_db.add(Document(id="ai-evo-north", doc_name="AI", minio_object="ai", tenant_id="north", chunk_count=2))
    test_db.add_all([
        Chunk(id="c0", doc_id="ai-evo-north", chunk_idx=0, content="保留"),
        Chunk(id="c2", doc_id="ai-evo-north", chunk_idx=2, content="撤回"),
        KnowledgeEvolutionDraft(id="d", tenant_id="north", status="indexed", chunk_id="c2"),
    ])
    await test_db.commit()

    async def delete_milvus(chunk):
        return None

    monkeypatch.setattr(evolution, "_delete_chunk_from_milvus", delete_milvus)
    await evolution.withdraw_draft(test_db, "d", "north")
    doc = await test_db.get(Document, "ai-evo-north")
    assert doc.chunk_count == 1
    assert (await test_db.execute(select(Chunk.id))).scalars().all() == ["c0"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["bm25", "generation", "semantic", "mysql"])
async def test_one_failed_post_commit_hook_does_not_skip_others(test_db, monkeypatch, cache_environment, failure):
    import app.config as config
    from app.core import metrics

    draft = KnowledgeEvolutionDraft(id="d", tenant_id="north", status="approved")
    test_db.add(draft)
    await test_db.commit()
    await _seed_cached_answer(test_db, "north", "旧查询")

    async def persist(db, row):
        return "new-chunk"

    async def fail_async(*args, **kwargs):
        raise RuntimeError("post-commit failure")

    def fail_sync():
        raise RuntimeError("post-commit failure")

    monkeypatch.setattr(evolution, "_persist_chunk_to_kb", persist)
    if failure == "bm25":
        monkeypatch.setattr(bm25_service, "mark_dirty", fail_sync)
    elif failure == "generation":
        monkeypatch.setattr(cache_environment.client, "incr", fail_async)
        monkeypatch.setattr(cache_environment.client, "eval", fail_async)
    elif failure == "semantic":
        monkeypatch.setattr(cache_environment.client, "delete", fail_async)
    else:
        monkeypatch.setattr(cache_persist, "cache_invalidate_for_doc_async", fail_async)
    before = config.citation_cache_version()
    counter = metrics.DEGRADED.labels(f"knowledge_changed_{failure}")
    before_degraded = counter._value.get()
    assert await evolution.reflow_to_kb(test_db, draft) == "new-chunk"
    assert draft.status == "indexed"
    assert config.citation_cache_version() != before
    if failure != "bm25":
        assert bm25_service._dirty
    if failure != "mysql":
        assert cache_environment.invalidated == ["ai-evo-north"]
    if failure != "semantic":
        assert (await semantic_cache.semantic_cache_get(None, "相似查询", tenant_id="north"))[0] is None
    assert counter._value.get() == before_degraded + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("store_type", ["memory", pytest.param("redis", marks=pytest.mark.integration)])
async def test_generation_catches_up_after_failed_persistence(monkeypatch, cache_environment, store_type):
    """Redis失败后恢复必须将持久代际追平，否则重启会重新命中上一代答案。"""
    import app.config as config
    from app.services.knowledge_change_service import knowledge_changed

    client = cache_environment.client
    if store_type == "redis":
        url = os.getenv("KNOWLEDGE_TEST_REDIS_URL")
        if not url:
            pytest.skip("需要独立、无业务数据的测试Redis")
        from redis.asyncio import Redis
        client = Redis.from_url(url, decode_responses=True)
        monkeypatch.setattr(redis_client, "get_redis", lambda: client)
    try:
        await client.set("qa:gov_gen", 5)
        monkeypatch.setattr(config, "_gov_generation", 5)

        async def unavailable(*args):
            raise ConnectionError("transient Redis outage")

        with monkeypatch.context() as outage:
            outage.setattr(client, "incr", unavailable)
            outage.setattr(client, "eval", unavailable)
            await knowledge_changed("ai-evo-north", tenant_id="north")
        assert config._gov_generation == 6
        assert int(await client.get("qa:gov_gen")) == 5
        old_key = await semantic_cache.get_cached_key(None, "故障期间的答案", "north")
        await redis_client.cache_set_json(old_key, {"answer": "已过期答案"}, 600)

        await knowledge_changed("ai-evo-north", tenant_id="north")
        assert config._gov_generation == 7
        persisted = int(await client.get("qa:gov_gen"))
        assert persisted == 7
        # 对应启动恢复后的消费行为：上一代实际Redis答案不应复活。
        config._gov_generation = persisted
        restored_key = await semantic_cache.get_cached_key(None, "故障期间的答案", "north")
        assert await redis_client.cache_get_json(restored_key) is None
        assert cache_environment.invalidated == ["ai-evo-north", "ai-evo-north"]
    finally:
        if store_type == "redis":
            await client.aclose()


@pytest.mark.asyncio
async def test_inflight_semantic_embedding_cannot_restore_old_generation(monkeypatch, cache_environment):
    from app.services.knowledge_change_service import knowledge_changed

    entered = asyncio.Event()
    resume = asyncio.Event()

    async def delayed_embedding(query):
        entered.set()
        await resume.wait()
        return [1.0, 0.0]

    old_key = await semantic_cache.get_cached_key(None, "旧答案问题", "north")
    await redis_client.cache_set_json(old_key, {"answer": "旧答案"}, 600)
    monkeypatch.setattr(semantic_cache.embedding_service, "embed_query", delayed_embedding)
    pending = asyncio.create_task(semantic_cache.semantic_cache_set(None, "旧答案问题", old_key, tenant_id="north"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await knowledge_changed("ai-evo-north", tenant_id="north")
    finally:
        resume.set()
        await pending
    assert not cache_environment.data.get("qa_semantic:tenant:north:index:v3")
    assert (await semantic_cache.semantic_cache_get(None, "相似查询", tenant_id="north"))[0] is None


@pytest.mark.asyncio
async def test_semantic_get_skips_stale_best_candidate_and_uses_current_one(cache_environment):
    import app.config as config

    old_key = await semantic_cache.get_cached_key(None, "旧问题", "north")
    await redis_client.cache_set_json(old_key, {"answer": "旧答案"}, 600)
    await semantic_cache.semantic_cache_set(None, "旧问题", old_key, [1.0, 0.0], tenant_id="north")
    config.bump_gov_generation_inproc()
    current_key = await semantic_cache.get_cached_key(None, "新问题", "north")
    await redis_client.cache_set_json(current_key, {"answer": "新答案"}, 600)
    await semantic_cache.semantic_cache_set(None, "新问题", current_key, [1.0, 0.1], tenant_id="north")
    cached, hit, _ = await semantic_cache.semantic_cache_get(None, "相似查询", tenant_id="north")
    assert cached == {"answer": "新答案"}
    assert hit == "semantic_high"


@pytest.mark.asyncio
@pytest.mark.parametrize("query,key", [("旧格式问题", "qa:north:default:旧格式问题"), ("问题:cv123", "qa:north:default:问题:cv123"), ("opaque", "legacy-key")])
async def test_semantic_cache_keeps_unversioned_legacy_entries(cache_environment, query, key):
    await redis_client.cache_set_json(key, {"answer": "兼容答案"}, 600)
    await semantic_cache.semantic_cache_set(None, query, key, [1.0, 0.0], tenant_id="north")
    cached, hit, _ = await semantic_cache.semantic_cache_get(None, "相似查询", tenant_id="north")
    assert cached == {"answer": "兼容答案"}
    assert hit == "semantic_high"


@pytest.mark.asyncio
@pytest.mark.parametrize("hit_type", ["exact", "semantic"])
async def test_cache_read_inflight_cannot_return_retired_generation(monkeypatch, cache_environment, hit_type):
    from app.services.knowledge_change_service import knowledge_changed

    old_key = await semantic_cache.get_cached_key(None, "旧问题", "north")
    await redis_client.cache_set_json(old_key, {"answer": "旧答案"}, 600)
    await semantic_cache.semantic_cache_set(None, "旧问题", old_key, [1.0, 0.0], tenant_id="north")
    entered, resume = asyncio.Event(), asyncio.Event()
    get_json = redis_client.cache_get_json

    async def delayed_get(key):
        cached = await get_json(key)
        if key == old_key:
            entered.set()
            await resume.wait()
        return cached

    monkeypatch.setattr(redis_client, "cache_get_json", delayed_get)
    pending = asyncio.create_task(semantic_cache.semantic_cache_get(
        None, "旧问题" if hit_type == "exact" else "相似查询", tenant_id="north",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await knowledge_changed("ai-evo-north", tenant_id="north")
    finally:
        resume.set()
    cached, hit, _ = await pending
    assert cached is None
    assert hit == "miss"
