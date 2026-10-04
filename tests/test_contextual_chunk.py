"""H2 切块上下文化单测：拼接、幂等、成本护栏、失败降级（不连 DB / 不打 LLM）。"""
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import contextual_chunk_service as ccs


class FakeDB:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _chunk(content="主变油温超过 85℃ 时应申请停运检查。", ctx="", section="5.3 油温异常处置"):
    return SimpleNamespace(content=content, context_line=ctx, section_path=section, section="")


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "CONTEXTUAL_CHUNK_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "CONTEXTUAL_CHUNK_MAX_PER_DOC", 500, raising=False)
    monkeypatch.setattr(settings, "CONTEXTUAL_CHUNK_CONCURRENCY", 2, raising=False)
    yield


def test_embed_text_for_prepends_context():
    assert ccs.embed_text_for("原文", "定位语") == "定位语\n原文"


def test_embed_text_for_without_context_is_raw_content():
    """无定位语（生成失败/未开启）→ 向量输入与现状完全一致。"""
    assert ccs.embed_text_for("原文", "") == "原文"
    assert ccs.embed_text_for("原文", None) == "原文"
    assert ccs.embed_text_for("原文", "   ") == "原文"


def test_enabled_follows_switch(monkeypatch):
    monkeypatch.setattr(settings, "CONTEXTUAL_CHUNK_ENABLE", False, raising=False)
    assert ccs.enabled() is False
    monkeypatch.setattr(settings, "CONTEXTUAL_CHUNK_ENABLE", True, raising=False)
    assert ccs.enabled() is True


@pytest.mark.asyncio
async def test_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "CONTEXTUAL_CHUNK_ENABLE", False, raising=False)
    db = FakeDB()
    assert await ccs.ensure_context_lines(db, SimpleNamespace(doc_name="d"), [_chunk()]) == 0
    assert db.commits == 0


@pytest.mark.asyncio
async def test_generates_and_persists(enabled, monkeypatch):
    async def fake_gen(model_type, doc_name, section, content):
        return f"{doc_name}中关于{section}的说明"

    monkeypatch.setattr(ccs, "_generate_one", fake_gen)
    db = FakeDB()
    chunks = [_chunk(), _chunk(content="另一段")]
    filled = await ccs.ensure_context_lines(db, SimpleNamespace(doc_name="主变压器运行规程"), chunks)

    assert filled == 2
    assert chunks[0].context_line == "主变压器运行规程中关于5.3 油温异常处置的说明"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_idempotent_skips_existing(enabled, monkeypatch):
    """已有定位语的 chunk 不重复生成（重跑不重复花钱）。"""
    calls = []

    async def fake_gen(model_type, doc_name, section, content):
        calls.append(content)
        return "新定位语"

    monkeypatch.setattr(ccs, "_generate_one", fake_gen)
    db = FakeDB()
    chunks = [_chunk(ctx="已有定位语"), _chunk(content="待处理")]
    filled = await ccs.ensure_context_lines(db, SimpleNamespace(doc_name="d"), chunks)

    assert filled == 1
    assert calls == ["待处理"]
    assert chunks[0].context_line == "已有定位语"


@pytest.mark.asyncio
async def test_max_per_doc_cap(enabled, monkeypatch):
    """成本护栏：单文档最多生成 N 条。"""
    monkeypatch.setattr(settings, "CONTEXTUAL_CHUNK_MAX_PER_DOC", 2, raising=False)

    async def fake_gen(model_type, doc_name, section, content):
        return "定位语"

    monkeypatch.setattr(ccs, "_generate_one", fake_gen)
    db = FakeDB()
    chunks = [_chunk(content=f"段{i}") for i in range(5)]
    filled = await ccs.ensure_context_lines(db, SimpleNamespace(doc_name="d"), chunks)

    assert filled == 2
    assert all(c.context_line == "" for c in chunks[2:])


@pytest.mark.asyncio
async def test_generation_failure_degrades_without_raising(enabled, monkeypatch):
    """单条生成失败 → 该条留空，其余照常，绝不抛出阻塞向量化。"""
    async def fake_gen(model_type, doc_name, section, content):
        if content == "坏段":
            raise RuntimeError("LLM down")
        return "定位语"

    monkeypatch.setattr(ccs, "_generate_one", fake_gen)
    db = FakeDB()
    chunks = [_chunk(content="好段"), _chunk(content="坏段")]
    filled = await ccs.ensure_context_lines(db, SimpleNamespace(doc_name="d"), chunks)

    assert filled == 1
    assert chunks[0].context_line == "定位语"
    assert chunks[1].context_line == ""


@pytest.mark.asyncio
async def test_generate_one_returns_empty_on_llm_error(monkeypatch):
    """_generate_one 自身吞异常返回空串（degrade 而非 crash）。"""
    class BoomProvider:
        async def chat(self, *a, **kw):
            raise RuntimeError("quota exceeded")

    monkeypatch.setattr("app.providers.factory.get_llm_provider", lambda mt=None: BoomProvider())
    assert await ccs._generate_one(None, "文档", "章节", "内容") == ""


@pytest.mark.asyncio
async def test_generate_one_strips_quotes(monkeypatch):
    class QuoteProvider:
        async def chat(self, *a, **kw):
            return "“这是定位语。”"

    monkeypatch.setattr("app.providers.factory.get_llm_provider", lambda mt=None: QuoteProvider())
    assert await ccs._generate_one(None, "文档", "章节", "内容") == "这是定位语。"
