"""Batch 2 · chunk 向量复用（A1/A4）单测。

EMBED_CHUNK_CACHE_ENABLE 默认关；关时 embed_texts 行为=现状（不缓存）。
开时按 chunk_id 查 Redis（chunk_embed:{provider}:{chunk_id}），命中跳过，miss embed+存。
缓存键含 provider；chunk_id 缺失/不传时走现状不缓存；缓存异常静默降级。
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch


def _run(coro):
    return asyncio.run(coro)


# ============ A1：embed_texts(chunk_ids=...) chunk_id 缓存 ============

def test_embed_texts_chunk_cache_disabled_by_default(monkeypatch):
    """默认关：传 chunk_ids 也不查 Redis（行为=现状）。"""
    from app.services import embedding_service
    from app.config import settings

    async def fake_embed(texts):
        return [[0.1, 0.2] for _ in texts]

    monkeypatch.setattr(settings, "EMBED_CHUNK_CACHE_ENABLE", False)
    monkeypatch.setattr(embedding_service, "get_embedding_provider",
                        lambda p=None: type("P", (), {"embed": staticmethod(fake_embed)})())

    async def go():
        with patch.object(embedding_service.redis_client, "get_redis") as mk:
            r = AsyncMock()
            r.get = AsyncMock(return_value=None)
            r.set = AsyncMock(return_value=True)
            mk.return_value = r
            vecs = await embedding_service.embed_texts(
                ["chunkA", "chunkB"],
                chunk_ids=["id1", "id2"],
            )
            assert len(vecs) == 2
            r.get.assert_not_called()    # 关 → 不查
            r.set.assert_not_called()    # 关 → 不写
    _run(go())


def test_embed_texts_chunk_cache_off_when_no_chunk_ids(monkeypatch):
    """开关开但 chunk_ids=None：走现状 embed（不缓存）。"""
    from app.services import embedding_service
    from app.config import settings

    async def fake_embed(texts):
        return [[0.3] for _ in texts]

    monkeypatch.setattr(settings, "EMBED_CHUNK_CACHE_ENABLE", True)
    monkeypatch.setattr(embedding_service, "get_embedding_provider",
                        lambda p=None: type("P", (), {"embed": staticmethod(fake_embed)})())

    async def go():
        with patch.object(embedding_service.redis_client, "get_redis") as mk:
            r = AsyncMock()
            r.get = AsyncMock(return_value=None)
            r.set = AsyncMock(return_value=True)
            mk.return_value = r
            vecs = await embedding_service.embed_texts(["q1", "q2"])  # 不传 chunk_ids
            assert len(vecs) == 2
            r.get.assert_not_called()
            r.set.assert_not_called()
    _run(go())


def test_embed_texts_chunk_cache_hit_skips_embed(monkeypatch):
    """开关开 + 传 chunk_ids：第二次同 chunk_id 命中缓存，embed 调用不增。"""
    from app.services import embedding_service
    from app.config import settings

    embed_calls = {"n": 0}

    async def fake_embed(texts):
        embed_calls["n"] += 1
        return [[0.5, 0.6] for _ in texts]

    monkeypatch.setattr(settings, "EMBED_CHUNK_CACHE_ENABLE", True)
    monkeypatch.setattr(settings, "EMBED_CHUNK_CACHE_TTL", 604800)
    monkeypatch.setattr(settings, "EMB_PROVIDER", "qwen")
    monkeypatch.setattr(embedding_service, "get_embedding_provider",
                        lambda p=None: type("P", (), {"embed": staticmethod(fake_embed)})())

    async def go():
        with patch.object(embedding_service.redis_client, "get_redis") as mk:
            r = AsyncMock()
            # 4 次 get：第 1 轮 miss,miss；第 2 轮 hit,hit
            r.get = AsyncMock(side_effect=[
                None, None,
                json.dumps([0.5, 0.6]), json.dumps([0.5, 0.6]),
            ])
            r.set = AsyncMock(return_value=True)
            mk.return_value = r

            v1 = await embedding_service.embed_texts(["c1", "c2"], chunk_ids=["id1", "id2"])
            v2 = await embedding_service.embed_texts(["c1", "c2"], chunk_ids=["id1", "id2"])

            assert len(v1) == len(v2) == 2
            assert embed_calls["n"] == 1        # 只第 1 轮 embed 一次（两次 texts 一批）
            assert r.set.call_count == 2        # 第 1 轮 miss → 存 2 条
    _run(go())


def test_embed_texts_chunk_cache_partial_hit(monkeypatch):
    """部分命中：命中用缓存，miss 才 embed，仅存 miss 的。"""
    from app.services import embedding_service
    from app.config import settings

    embed_calls = {"n": 0}

    async def fake_embed(texts):
        embed_calls["n"] += 1
        return [[0.9] for _ in texts]

    monkeypatch.setattr(settings, "EMBED_CHUNK_CACHE_ENABLE", True)
    monkeypatch.setattr(settings, "EMB_PROVIDER", "qwen")
    monkeypatch.setattr(embedding_service, "get_embedding_provider",
                        lambda p=None: type("P", (), {"embed": staticmethod(fake_embed)})())

    async def go():
        with patch.object(embedding_service.redis_client, "get_redis") as mk:
            r = AsyncMock()
            # id1 命中，id2 miss
            r.get = AsyncMock(side_effect=[json.dumps([0.9]), None])
            r.set = AsyncMock(return_value=True)
            mk.return_value = r
            vecs = await embedding_service.embed_texts(
                ["c1", "c2"], chunk_ids=["id1", "id2"],
            )
            assert vecs[0] == [0.9]            # 来自缓存
            assert vecs[1] == [0.9]            # 来自 embed
            assert embed_calls["n"] == 1
            # 仅 embed 1 条（id2），但 embed 调一次 batch=[c2]
            r.set.assert_called_once()
            _key, _val = r.set.call_args.args[0], r.set.call_args.args[1]
            assert "id2" in _key               # 存的是 miss 的 id2
    _run(go())


def test_embed_texts_chunk_cache_key_includes_provider(monkeypatch):
    """缓存键含 provider（云/bge 向量空间不同，不混）。"""
    from app.services import embedding_service
    from app.config import settings

    async def fake_embed(texts):
        return [[0.1] for _ in texts]

    monkeypatch.setattr(settings, "EMBED_CHUNK_CACHE_ENABLE", True)
    monkeypatch.setattr(settings, "EMB_PROVIDER", "qwen")
    monkeypatch.setattr(embedding_service, "get_embedding_provider",
                        lambda p=None: type("P", (), {"embed": staticmethod(fake_embed)})())

    async def go():
        with patch.object(embedding_service.redis_client, "get_redis") as mk:
            r = AsyncMock()
            r.get = AsyncMock(return_value=None)
            r.set = AsyncMock(return_value=True)
            mk.return_value = r
            await embedding_service.embed_texts(["c1"], chunk_ids=["id1"])
            key = r.set.call_args.args[0]
            assert key == "chunk_embed:qwen:id1"
    _run(go())


def test_embed_texts_chunk_cache_mismatched_lengths_fallback(monkeypatch):
    """chunk_ids 长度 != texts 长度 → 不缓存，走现状 embed（防御）。"""
    from app.services import embedding_service
    from app.config import settings

    async def fake_embed(texts):
        return [[0.1] for _ in texts]

    monkeypatch.setattr(settings, "EMBED_CHUNK_CACHE_ENABLE", True)
    monkeypatch.setattr(embedding_service, "get_embedding_provider",
                        lambda p=None: type("P", (), {"embed": staticmethod(fake_embed)})())

    async def go():
        with patch.object(embedding_service.redis_client, "get_redis") as mk:
            r = AsyncMock()
            r.get = AsyncMock(return_value=None)
            r.set = AsyncMock(return_value=True)
            mk.return_value = r
            vecs = await embedding_service.embed_texts(
                ["c1", "c2"], chunk_ids=["only_one_id"],
            )
            assert len(vecs) == 2
            r.get.assert_not_called()
            r.set.assert_not_called()
    _run(go())


def test_embed_texts_chunk_cache_get_error_silent(monkeypatch):
    """Redis get 异常 → 静默降级（不阻塞，仍 embed）。"""
    from app.services import embedding_service
    from app.config import settings

    async def fake_embed(texts):
        return [[0.1] for _ in texts]

    monkeypatch.setattr(settings, "EMBED_CHUNK_CACHE_ENABLE", True)
    monkeypatch.setattr(embedding_service, "get_embedding_provider",
                        lambda p=None: type("P", (), {"embed": staticmethod(fake_embed)})())

    async def go():
        with patch.object(embedding_service.redis_client, "get_redis") as mk:
            r = AsyncMock()
            r.get = AsyncMock(side_effect=RuntimeError("redis down"))
            r.set = AsyncMock(return_value=True)
            mk.return_value = r
            vecs = await embedding_service.embed_texts(["c1"], chunk_ids=["id1"])
            assert vecs == [[0.1]]
    _run(go())


def test_embed_texts_chunk_cache_empty_chunk_id_skipped(monkeypatch):
    """chunk_id 为空串 → 不查不存（chunk_id 缺失走现状）。"""
    from app.services import embedding_service
    from app.config import settings

    async def fake_embed(texts):
        return [[0.1] for _ in texts]

    monkeypatch.setattr(settings, "EMBED_CHUNK_CACHE_ENABLE", True)
    monkeypatch.setattr(embedding_service, "get_embedding_provider",
                        lambda p=None: type("P", (), {"embed": staticmethod(fake_embed)})())

    async def go():
        with patch.object(embedding_service.redis_client, "get_redis") as mk:
            r = AsyncMock()
            r.get = AsyncMock(return_value=None)
            r.set = AsyncMock(return_value=True)
            mk.return_value = r
            await embedding_service.embed_texts(["c1", "c2"], chunk_ids=["", "id2"])
            # 只查 id2（id1 空→跳过）
            assert r.get.call_count == 1
            got_key = r.get.call_args.args[0]
            assert "id2" in got_key
    _run(go())


# ============ A4：citation_verifier / auto_cite 传 chunk_id 复用 ============

def test_citation_verifier_passes_chunk_ids_to_embed(monkeypatch):
    """校验2：verify 把 CitationItem.chunk_id 列表传给 embed_texts（chunk 向量复用）。"""
    from app.config import settings

    captured = {}

    async def fake_embed(texts, chunk_ids=None):
        captured.setdefault("calls", []).append((list(texts), chunk_ids))
        # 句向量 vs chunk 向量都放行
        return [[1.0] for _ in texts]

    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    monkeypatch.setattr(settings, "CITATION_NLI_ENABLE", False)

    from app.rag.citation_verifier import verify
    from app.schemas.citation import CitationItem

    cmap = [CitationItem(sentence="s1", ref_id=1, chunk_id="c1"),
            CitationItem(sentence="s2", ref_id=2, chunk_id="c2")]
    contexts = [{"chunkId": "c1", "chunk": "x1"}, {"chunkId": "c2", "chunk": "x2"}]
    _run(verify("s1[1] s2[2]", cmap, {1: "c1", 2: "c2"}, contexts, "deepseek",
               nli_enable=False))
    # 第 1 次：句向量（无 chunk_ids）；第 2 次：chunk 向量（chunk_ids=c1/c2）
    assert len(captured["calls"]) == 2
    chunk_call = captured["calls"][1]
    assert chunk_call[1] == ["c1", "c2"]


def test_auto_cite_passes_chunk_ids_to_embed(monkeypatch):
    """auto_cite：contexts[i].chunkId 透传给 embed_texts（chunk 向量复用）。"""
    captured = {}

    async def fake_embed(texts, chunk_ids=None):
        captured.setdefault("calls", []).append((list(texts), chunk_ids))
        if len(captured["calls"]) == 1:           # 第 1 次：chunks
            return [[1.0, 0.0], [0.0, 1.0]]
        return [[0.9, 0.1]]                       # bare 句偏向 chunk0

    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    from app.rag import citation

    answer = "油温过高需停运。"
    contexts = [
        {"chunk": "油温限值", "chunkId": "c1", "docName": "A"},
        {"chunk": "停运流程", "chunkId": "c2", "docName": "B"},
    ]
    annotated, _ = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[1]" in annotated
    # chunks 调用带 chunk_ids=["c1","c2"]；bare 句子调用不带（无 chunk_id）
    assert captured["calls"][0][1] == ["c1", "c2"]
    assert captured["calls"][1][1] is None


def test_embed_texts_signature_backward_compat(monkeypatch):
    """旧 caller 不传 chunk_ids 时签名兼容（positional 列表仍工作）。"""
    from app.services import embedding_service
    from app.config import settings

    async def fake_embed(texts):
        return [[0.1] for _ in texts]

    monkeypatch.setattr(settings, "EMBED_CHUNK_CACHE_ENABLE", True)
    monkeypatch.setattr(embedding_service, "get_embedding_provider",
                        lambda p=None: type("P", (), {"embed": staticmethod(fake_embed)})())

    async def go():
        with patch.object(embedding_service.redis_client, "get_redis") as mk:
            r = AsyncMock()
            r.get = AsyncMock(return_value=None)
            mk.return_value = r
            # 旧 positional 调用：embed_texts(["a","b"])
            v = await embedding_service.embed_texts(["a", "b"])
            assert len(v) == 2
    _run(go())
