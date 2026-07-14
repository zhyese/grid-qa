"""CRAG v2 per-doc 分级单测与强健解析测试。"""
import pytest
import asyncio
from app.rag import crag, crag_v2
from app.rag.crag_v2 import labels_to_grade, _parse_llm_json, grade_with_llm
from app.config import settings


def test_two_relevant_is_correct():
    g, d = labels_to_grade(["relevant", "relevant", "irrelevant"])
    assert g == crag.GRADE_CORRECT
    assert d["relevant"] == 2


def test_one_relevant_is_ambiguous():
    assert labels_to_grade(["relevant", "irrelevant"])[0] == crag.GRADE_AMBIGUOUS


def test_one_partial_is_ambiguous():
    assert labels_to_grade(["partial", "irrelevant"])[0] == crag.GRADE_AMBIGUOUS


def test_all_irrelevant_is_incorrect():
    g, d = labels_to_grade(["irrelevant", "irrelevant", "irrelevant"])
    assert g == crag.GRADE_INCORRECT
    assert d["irrelevant"] == 3


def test_empty_is_incorrect():
    """无任何相关证据 → incorrect（触发 CRAG 纠错闭环）。"""
    assert labels_to_grade([])[0] == crag.GRADE_INCORRECT


def test_parse_llm_json_clean():
    raw = '{"verdicts": [{"idx": 1, "label": "relevant"}]}'
    res = _parse_llm_json(raw)
    assert res == {"verdicts": [{"idx": 1, "label": "relevant"}]}


def test_parse_llm_json_markdown():
    raw = '```json\n{"verdicts": [{"idx": 1, "label": "relevant"}]}\n```'
    res = _parse_llm_json(raw)
    assert res == {"verdicts": [{"idx": 1, "label": "relevant"}]}


def test_parse_llm_json_conversational():
    raw = 'Sure, here is the result:\n{\n  "verdicts": [\n    {"idx": 1, "label": "relevant"}\n  ]\n}\nHope this helps!'
    res = _parse_llm_json(raw)
    assert res == {"verdicts": [{"idx": 1, "label": "relevant"}]}


def test_parse_llm_json_invalid():
    raw = 'this is not json at all'
    res = _parse_llm_json(raw)
    assert res is None


class _FakeProvider:
    def __init__(self, resp=None, delay=0.0, raise_timeout=False):
        self.resp = resp
        self.delay = delay
        self.raise_timeout = raise_timeout

    async def chat(self, messages, **kwargs):
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.raise_timeout:
            raise asyncio.TimeoutError("Mock timeout")
        return self.resp


def test_grade_with_llm_happy_path(monkeypatch):
    monkeypatch.setattr(settings, "CRAG_PERDOC_ENABLE", True)
    
    fake_response = '{"verdicts": [{"idx": 1, "label": "relevant"}, {"idx": 2, "label": "relevant"}]}'
    monkeypatch.setattr(crag_v2, "get_llm_provider", lambda mt=None: _FakeProvider(resp=fake_response))
    
    contexts = [{"chunk": "doc1"}, {"chunk": "doc2"}]
    grade, detail = asyncio.run(grade_with_llm("test query", contexts))
    assert grade == crag.GRADE_CORRECT
    assert detail["relevant"] == 2


def test_grade_with_llm_timeout(monkeypatch):
    monkeypatch.setattr(settings, "CRAG_PERDOC_ENABLE", True)
    monkeypatch.setattr(settings, "CRAG_TIMEOUT", 0.1)
    
    # Mock provider that sleeps for 0.5s (longer than the 0.1s timeout)
    monkeypatch.setattr(crag_v2, "get_llm_provider", lambda mt=None: _FakeProvider(resp="{}", delay=0.5))
    
    contexts = [{"chunk": "doc1"}]
    grade, detail = asyncio.run(grade_with_llm("test query", contexts))
    assert grade == ""
    assert detail == {}


def test_grade_with_llm_invalid_json(monkeypatch):
    monkeypatch.setattr(settings, "CRAG_PERDOC_ENABLE", True)
    
    monkeypatch.setattr(crag_v2, "get_llm_provider", lambda mt=None: _FakeProvider(resp="invalid json response"))
    
    contexts = [{"chunk": "doc1"}]
    grade, detail = asyncio.run(grade_with_llm("test query", contexts))
    assert grade == ""
    assert detail == {}
