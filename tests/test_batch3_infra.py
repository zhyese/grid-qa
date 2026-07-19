"""Batch 3 数据链路基础设施单测（B1/B2/B3/B4/B5）。

opt-in 默认关；关时=现状零破坏。
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


# ============ B1：chunks 复合索引（doc_id, chunk_idx） ============

def test_chunk_has_doc_idx_composite_index():
    """Chunk 模型声明了 (doc_id, chunk_idx) 复合索引。"""
    from app.models.chunk import Chunk
    indexes = [tuple(c.name for c in idx.columns) for idx in Chunk.__table_args__]
    assert ("doc_id", "parent_idx") in indexes  # 既有
    assert ("doc_id", "chunk_idx") in indexes    # B1 新增


def test_init_db_has_chunks_doc_idx_index_migration():
    """init_db _INDEX_MIGRATIONS 含 ix_chunks_doc_idx（CREATE INDEX 幂等）。"""
    import app.db.init_db as init_db
    names = [entry[1] for entry in init_db._INDEX_MIGRATIONS]
    assert "ix_chunks_doc_idx" in names


# ============ B2：缓存命中滑动续期（CACHE_SLIDE_TTL_ENABLE，默认关） ============

def test_cache_get_json_slide_off_by_default():
    """默认关：cache_get_json 命中不 EXPIRE。"""
    from app.clients import redis_client
    from app.config import settings

    async def go():
        with patch.object(redis_client, "get_redis") as mk, \
             patch.object(settings, "CACHE_SLIDE_TTL_ENABLE", False):
            r = AsyncMock()
            r.get = AsyncMock(return_value=json.dumps({"a": 1}))
            r.expire = AsyncMock(return_value=True)
            mk.return_value = r
            v = await redis_client.cache_get_json("k")
            assert v == {"a": 1}
            r.expire.assert_not_called()  # 关 → 不续期
    asyncio.run(go())


def test_cache_get_json_slide_on_refreshes_ttl():
    """开关开：命中时 EXPIRE 刷新 TTL=QA_CACHE_TTL。"""
    from app.clients import redis_client
    from app.config import settings

    async def go():
        with patch.object(redis_client, "get_redis") as mk, \
             patch.object(settings, "CACHE_SLIDE_TTL_ENABLE", True), \
             patch.object(settings, "QA_CACHE_TTL", 259200):
            r = AsyncMock()
            r.get = AsyncMock(return_value=json.dumps({"a": 1}))
            r.expire = AsyncMock(return_value=True)
            mk.return_value = r
            v = await redis_client.cache_get_json("k")
            assert v == {"a": 1}
            r.expire.assert_called_once_with("k", 259200)
    asyncio.run(go())


def test_cache_get_json_slide_miss_no_expire():
    """开关开但 miss：不 EXPIRE。"""
    from app.clients import redis_client
    from app.config import settings

    async def go():
        with patch.object(redis_client, "get_redis") as mk, \
             patch.object(settings, "CACHE_SLIDE_TTL_ENABLE", True):
            r = AsyncMock()
            r.get = AsyncMock(return_value=None)
            r.expire = AsyncMock(return_value=True)
            mk.return_value = r
            v = await redis_client.cache_get_json("k")
            assert v is None
            r.expire.assert_not_called()
    asyncio.run(go())


def test_cache_get_json_slide_expire_error_silent():
    """EXPIRE 异常静默吞掉（不阻塞读）。"""
    from app.clients import redis_client
    from app.config import settings

    async def go():
        with patch.object(redis_client, "get_redis") as mk, \
             patch.object(settings, "CACHE_SLIDE_TTL_ENABLE", True):
            r = AsyncMock()
            r.get = AsyncMock(return_value=json.dumps({"a": 1}))
            r.expire = AsyncMock(side_effect=RuntimeError("net err"))
            mk.return_value = r
            v = await redis_client.cache_get_json("k")
            assert v == {"a": 1}  # 仍返回值
    asyncio.run(go())


# ============ B3：embedding 命中续期（EMBED_CACHE_SLIDE_TTL_ENABLE，默认关） ============

def test_embed_query_slide_off_by_default():
    """默认关：embed_query 命中不 EXPIRE。"""
    from app.services import embedding_service
    from app.config import settings

    async def go():
        with patch.object(embedding_service.redis_client, "get_redis") as mk, \
             patch.object(settings, "EMBED_CACHE_SLIDE_TTL_ENABLE", False):
            r = AsyncMock()
            r.get = AsyncMock(return_value=json.dumps([0.1, 0.2]))
            r.expire = AsyncMock(return_value=True)
            mk.return_value = r
            vec = await embedding_service.embed_query("q")
            assert vec == [0.1, 0.2]
            r.expire.assert_not_called()
    asyncio.run(go())


def test_embed_query_slide_on_refreshes_ttl():
    """开关开：embed_query 命中时 EXPIRE 续期。"""
    from app.services import embedding_service
    from app.config import settings

    async def go():
        with patch.object(embedding_service.redis_client, "get_redis") as mk, \
             patch.object(settings, "EMBED_CACHE_SLIDE_TTL_ENABLE", True), \
             patch.object(settings, "EMBED_CACHE_TTL", 3600):
            r = AsyncMock()
            r.get = AsyncMock(return_value=json.dumps([0.1, 0.2]))
            r.expire = AsyncMock(return_value=True)
            mk.return_value = r
            vec = await embedding_service.embed_query("q")
            assert vec == [0.1, 0.2]
            r.expire.assert_called_once_with("emb:qwen:q", 3600)
    asyncio.run(go())


# ============ B4：真实 token usage（chat_with_usage 副通道，LLM_USAGE_TRACK_ENABLE，默认关） ============

def _make_resp_with_usage(content, prompt_tokens, completion_tokens):
    msg = SimpleNamespace(content=content, tool_calls=None)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def _make_resp_no_usage(content):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


def test_chat_with_usage_returns_real_tokens():
    """openai SDK 响应 usage 透传（DeepSeek 示例）。"""
    from app.providers.llm.deepseek_llm import DeepSeekLLM
    p = DeepSeekLLM()

    async def fake_create(**kw):
        return _make_resp_with_usage("答案", 120, 35)

    p.client.chat.completions.create = fake_create
    content, usage = asyncio.run(p.chat_with_usage([{"role": "user", "content": "q"}]))
    assert content == "答案"
    assert usage == {"input": 120, "output": 35}


def test_chat_with_usage_no_usage_returns_none():
    """响应无 usage（兼容老接口）→ 返回 None。"""
    from app.providers.llm.qwen_llm import QwenLLM
    p = QwenLLM()

    async def fake_create(**kw):
        return _make_resp_no_usage("答案")

    p.client.chat.completions.create = fake_create
    content, usage = asyncio.run(p.chat_with_usage([{"role": "user", "content": "q"}]))
    assert content == "答案"
    assert usage is None


def test_chat_str_contract_unchanged():
    """chat 仍返回 str（向后兼容，零破坏）。"""
    from app.providers.llm.doubao_llm import DoubaoLLM
    p = DoubaoLLM()

    async def fake_create(**kw):
        return _make_resp_with_usage("答案", 10, 5)

    p.client.chat.completions.create = fake_create
    out = asyncio.run(p.chat([{"role": "user", "content": "q"}]))
    assert isinstance(out, str)
    assert out == "答案"


def test_chat_with_usage_base_default_returns_none():
    """LLMProvider 基类默认 chat_with_usage 回退到 chat，usage=None。"""
    from app.providers.base import LLMProvider

    class Dummy(LLMProvider):
        async def chat(self, messages, temperature=0.2, max_tokens=2048, **kw) -> str:
            return "hi"

    d = Dummy()
    content, usage = asyncio.run(d.chat_with_usage([{"role": "user", "content": "q"}]))
    assert content == "hi"
    assert usage is None


# ============ B5：GraphRAG jieba 分词缓存（KG_TOKENIZE_CACHE_ENABLE，默认关） ============

def test_kg_tokenize_cache_off_by_default():
    """默认关：每次走 jieba.cut。"""
    from app.services import kg_service
    from app.config import settings

    async def go():
        called = {"n": 0}

        def fake_jieba_cut(q):
            called["n"] += 1
            return ["主变", "油温", "高"]

        with patch.object(settings, "KG_TOKENIZE_CACHE_ENABLE", False), \
             patch.object(kg_service.redis_client, "get_redis") as mk:
            r = AsyncMock()
            r.get = AsyncMock(return_value=None)
            r.set = AsyncMock(return_value=True)
            mk.return_value = r
            with patch("jieba.cut", fake_jieba_cut):
                words1 = await kg_service._tokenize_query("主变油温高")
                words2 = await kg_service._tokenize_query("主变油温高")
            # 单字 "高" 被 len>1 过滤
            assert words1 == ["主变", "油温"]
            assert words2 == ["主变", "油温"]
            assert called["n"] == 2  # 每次都 jieba
            r.get.assert_not_called()
            r.set.assert_not_called()
    asyncio.run(go())


def test_kg_tokenize_cache_on_hits_redis():
    """开关开：第二次查 Redis 命中（jieba 不再调）。"""
    from app.services import kg_service
    from app.config import settings

    async def go():
        called = {"n": 0}

        def fake_jieba_cut(q):
            called["n"] += 1
            return ["主变", "油温", "高"]

        with patch.object(settings, "KG_TOKENIZE_CACHE_ENABLE", True), \
             patch.object(kg_service.redis_client, "get_redis") as mk:
            r = AsyncMock()
            # 第一次 miss（None），第二次 hit（缓存的过滤后列表）
            r.get = AsyncMock(side_effect=[None, json.dumps(["主变", "油温"])])
            r.set = AsyncMock(return_value=True)
            mk.return_value = r
            with patch("jieba.cut", fake_jieba_cut):
                words1 = await kg_service._tokenize_query("主变油温高")
                words2 = await kg_service._tokenize_query("主变油温高")
            assert words1 == ["主变", "油温"]
            assert words2 == ["主变", "油温"]
            assert called["n"] == 1  # 只调一次
            assert r.set.call_count == 1  # miss 时写一次
    asyncio.run(go())
