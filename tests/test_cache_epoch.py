"""知识更新与在途 QA/NLI/L2 请求交错时，不得把旧答案提升到新缓存代际。"""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import config
from app.models.qa_cache import QaCache
from app.services import cache_persist, qa_service


@pytest.fixture
def cache_store(monkeypatch):
    store = {}
    monkeypatch.setattr(config, "_gov_generation", 0)

    async def get(key):
        return store.get(key)

    async def put(key, value, ttl):
        store[key] = value

    monkeypatch.setattr(qa_service.redis_client, "cache_get_json", get)
    monkeypatch.setattr(qa_service.redis_client, "cache_set_json", put)
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize("change_at", ["read", "commit"])
async def test_l2_read_crossing_epoch_is_miss_without_redis_backfill(
    test_db, cache_store, monkeypatch, change_at,
):
    await cache_persist.cache_set_mysql(test_db, None, "问题", "问题", {"answer": "旧答案"})
    original = getattr(test_db, "execute" if change_at == "read" else "commit")

    async def interleave(*args, **kwargs):
        result = await original(*args, **kwargs)
        config.bump_gov_generation_inproc()
        return result

    monkeypatch.setattr(test_db, "execute" if change_at == "read" else "commit", interleave)
    result = await cache_persist.cache_get_mysql(test_db, None, "问题")
    await asyncio.sleep(0)

    assert result is None
    assert cache_store == {}


@pytest.mark.asyncio
async def test_l2_rejects_expired_expected_epoch(test_db, cache_store):
    version = config.citation_cache_version()
    config.bump_gov_generation_inproc()
    await cache_persist.cache_set_mysql(
        test_db, None, "问题", "问题", {"answer": "旧答案"}, expected_version=version,
    )
    assert (await test_db.execute(select(QaCache))).scalars().all() == []


@pytest.mark.asyncio
async def test_l2_write_rechecks_epoch_after_lookup(test_db, cache_store, monkeypatch):
    original = test_db.execute
    first = True

    async def interleave(*args, **kwargs):
        nonlocal first
        result = await original(*args, **kwargs)
        if first:
            first = False
            config.bump_gov_generation_inproc()
        return result

    monkeypatch.setattr(test_db, "execute", interleave)
    await cache_persist.cache_set_mysql(test_db, None, "问题", "问题", {"answer": "旧答案"})
    assert (await original(select(QaCache))).scalars().all() == []


@pytest.fixture
def qa_external_boundaries(monkeypatch):
    from app.services import agent_personas, agent_runtime, cost_tracker_service, online_eval_service, persona_store

    for name in ("HOTQA_ENABLE", "KG_RAG_ENABLE", "ROUTING_ENABLE", "SELF_RAG_ENABLE",
                 "SEMANTIC_CACHE_ENABLE", "EVIDENCE_GAP_AUTO_COLLECT", "CITATION_AUTO_ENABLE",
                 "CITATION_VERIFIER_ENABLE", "CITATION_STRUCTURED_OUTPUT", "LLM_USAGE_TRACK_ENABLE",
                 "PROMPT_CACHE_ENABLE"):
        monkeypatch.setattr(config.settings, name, False)
    monkeypatch.setattr(config.settings, "CACHE_PERSIST_ENABLE", True)
    ctx = [{"chunkId": "c1", "docId": "d1", "chunk": "原有证据", "docName": "规程", "score": 0.9}]

    async def retrieve(*args, **kwargs):
        return ctx

    async def crag(*args, **kwargs):
        return ctx, "high", "none", "good", {}

    async def not_blacklisted(*args):
        return False

    async def no_io(*args, **kwargs):
        return None

    async def persona(name):
        return agent_personas.QA_PERSONA

    monkeypatch.setattr(qa_service.retrieval_service, "mixed_search", retrieve)
    monkeypatch.setattr(qa_service, "_crag_correct", crag)
    monkeypatch.setattr(qa_service, "_is_blacklisted", not_blacklisted)
    monkeypatch.setattr(qa_service, "_write_highbase", no_io)
    monkeypatch.setattr(cost_tracker_service, "record_token_usage", no_io)
    monkeypatch.setattr(online_eval_service, "should_sample", lambda: False)
    monkeypatch.setattr(persona_store, "get_persona", persona)
    return agent_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["answer", "stream", "agent"])
@pytest.mark.parametrize("changed", [False, True, "after_mysql"])
async def test_qa_generation_never_promotes_old_evidence_to_new_epoch(
    test_db, cache_store, qa_external_boundaries, monkeypatch, mode, changed,
):
    initial_version = config.citation_cache_version()
    if changed == "after_mysql":
        original_set = cache_persist.cache_set_mysql

        async def persist_then_change(*args, **kwargs):
            await original_set(*args, **kwargs)
            config.bump_gov_generation_inproc()

        monkeypatch.setattr(cache_persist, "cache_set_mysql", persist_then_change)

    def answer_text():
        if changed is True:
            config.bump_gov_generation_inproc()
        return "依据原有证据生成的答案"

    class LLM:
        async def chat(self, *args, **kwargs):
            return answer_text()

        async def stream(self, *args, **kwargs):
            yield answer_text()

    async def agent(*args, **kwargs):
        return SimpleNamespace(answer=answer_text(), degraded=False, iterations=1, tools_used=[])

    monkeypatch.setattr(qa_service, "get_llm_provider", lambda *a, **kw: LLM())
    monkeypatch.setattr(qa_external_boundaries, "run_agent", agent)
    if mode == "answer":
        result = await qa_service.answer(test_db, "设备规程", "deepseek", tenant="tenant-a", regen=True)
        assert result["answer"] == "依据原有证据生成的答案"
    else:
        events = [event async for event in qa_service.stream_answer(
            test_db, "设备规程", "deepseek", tenant="tenant-a", regen=True, agent_mode=mode == "agent",
        )]
        assert any(event.get("content") == "依据原有证据生成的答案" for event in events)
    await asyncio.sleep(0)
    rows = (await test_db.execute(select(QaCache))).scalars().all()
    if changed == "after_mysql":
        assert len(rows) == 1
        assert rows[0].cache_key.endswith(":" + initial_version)
        assert cache_store == {}
    elif changed:
        assert rows == []
        assert cache_store == {}
    else:
        assert len(rows) == 1
        assert list(cache_store) == [rows[0].cache_key]
        assert rows[0].query_hash == QaCache.build_hash("deepseek", "设备规程", "tenant-a")


@pytest.mark.asyncio
@pytest.mark.parametrize("change_at", ["scheduled", "judge", "read"])
async def test_nli_backfill_does_not_patch_new_epoch_answer(cache_store, monkeypatch, change_at):
    from app.rag import judge
    from app.schemas.citation import CitationItem, VerifyItem, VerifyResult

    written = []

    async def verify(*args, **kwargs):
        if change_at == "judge":
            config.bump_gov_generation_inproc()
        return [{"label": "support"}]

    async def get(key):
        if change_at == "read":
            config.bump_gov_generation_inproc()
        return {"answer": "新知识答案", "citationVerified": {"items": [{"ref_id": 1}]}}

    async def put(key, value, ttl):
        written.append(key)

    monkeypatch.setattr(judge, "_verify_claims", verify)
    monkeypatch.setattr(qa_service.redis_client, "cache_get_json", get)
    monkeypatch.setattr(qa_service.redis_client, "cache_set_json", put)
    cmap = [CitationItem(sentence="原有证据", ref_id=1, chunk_id="c1")]
    verdict = VerifyResult(items=[VerifyItem(ref_id=1, chunk_id="c1", valid=True, action="keep")])
    args = (None, cmap, [{"chunkId": "c1", "chunk": "原有证据"}], "问题", "default", verdict)
    if change_at == "scheduled":
        qa_service._schedule_nli_backfill(*args)
        config.bump_gov_generation_inproc()
        await asyncio.gather(*tuple(qa_service._bg_tasks))
    else:
        await qa_service._nli_backfill(*args)
    assert written == []
