"""证据溯源 auto_cite + 配置项单测。"""
import asyncio

from app.config import settings
from app.rag import citation


def _run(coro):
    return asyncio.run(coro)


def test_citation_settings_defaults():
    assert settings.CITATION_AUTO_ENABLE is True
    assert settings.CITATION_SIM_THRESHOLD == 0.6
    # 新开关默认全 opt-in（关闭=现状）
    assert settings.CITATION_VERIFIER_ENABLE is False
    assert settings.CITATION_NLI_ENABLE is False
    assert settings.CITATION_NLI_TIMEOUT == 5
    assert settings.CITATION_STRUCTURED_OUTPUT is False
    assert settings.CITATION_REWRITE_ON_FAIL is True


def test_auto_cite_all_already_cited(monkeypatch):
    """答案每句已有角标 → 不再补，trace 全支撑。"""
    async def fake_embed(texts):
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    answer = "主变油温应≤85℃[1]。应申请停运[1]。"
    contexts = [{"chunk": "主变油温限值85", "docName": "A"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[1]" in annotated
    assert trace["totalSupported"] == 2
    assert trace["supportRatio"] == 1.0


def test_auto_cite_bare_sentence_matched(monkeypatch):
    """无角标句子 → 补到最相似 chunk。"""
    calls = []

    async def fake_embed(texts):
        calls.append(texts)
        if len(calls) == 1:           # 第一次：chunks
            return [[1.0, 0.0], [0.0, 1.0]]
        return [[0.9, 0.1]]           # bare 句偏向 chunk0

    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    answer = "油温过高需停运。"        # 无角标
    contexts = [{"chunk": "油温限值", "docName": "A"}, {"chunk": "停运流程", "docName": "B"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[1]" in annotated                   # 补到 chunk0 → [1]（标点前）
    assert trace["supportRatio"] == 1.0


def test_auto_cite_below_threshold_not_annotated(monkeypatch):
    """句子与所有 chunk 相似度都低于阈值 → 不补，保留无引用。"""
    calls = []

    async def fake_embed(texts):
        calls.append(texts)
        if len(calls) == 1:
            return [[1.0, 0.0], [0.9, 0.4359]]   # 两 chunk 近乎同向
        return [[0.0, 1.0]]                       # 句子与两 chunk 都低相关

    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    answer = "完全无关的一句话。"
    contexts = [{"chunk": "x", "docName": "A"}, {"chunk": "y", "docName": "B"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[" not in annotated                   # 没补任何角标
    assert trace["totalSupported"] == 0


def test_auto_cite_embed_failure_degrades(monkeypatch):
    """embed 异常 → 降级，返回原答案 + 仅原有角标 trace，不抛。"""
    async def boom(texts):
        raise RuntimeError("embed down")
    monkeypatch.setattr("app.services.embedding_service.embed_texts", boom)
    answer = "有据[1]。无据句。"
    contexts = [{"chunk": "x", "docName": "A"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[1]" in annotated
    assert trace["totalSupported"] == 1           # 只有原本带角标那句
    assert trace["totalSentences"] == 2


def test_auto_cite_empty_contexts():
    """无 contexts → 原样返回，不调 embed。"""
    annotated, trace = _run(citation.auto_cite("某句。", [], threshold=0.6))
    assert "[" not in annotated
    assert trace["totalSupported"] == 0
