"""DeepSeek 推理型模型（deepseek-flash/v4-pro）兼容单测：reasoning_content 兜底。

新 key 下的可用模型全是推理型：答案先流 reasoning_content 后出 content，
LLM_MAX_TOKENS 有限时 content 可能全程为空。验证三个出口（非流式/流式/工具）
在 content 空时用 reasoning 兜底、正常 content 优先不受影响。
"""
from types import SimpleNamespace

import pytest

from app.providers.llm.deepseek_llm import DeepSeekLLM, _extract_content

pytestmark = pytest.mark.asyncio


def _msg(content="", reasoning=""):
    return SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=None)


def _resp(message):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)],
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


class _FakeCompletions:
    def __init__(self, ret):
        self._ret = ret

    async def create(self, **kw):
        return self._ret


def _provider(monkeypatch, ret):
    p = DeepSeekLLM()
    monkeypatch.setattr(p, "client",
                        SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(ret))))
    return p


async def test_chat_content_preferred(monkeypatch):
    p = _provider(monkeypatch, _resp(_msg(content="正式答案", reasoning="推理过程")))
    assert await p.chat([{"role": "user", "content": "q"}]) == "正式答案"


async def test_chat_reasoning_fallback_when_content_empty(monkeypatch):
    p = _provider(monkeypatch, _resp(_msg(content="", reasoning="推理出的答案")))
    assert await p.chat([{"role": "user", "content": "q"}]) == "推理出的答案"


async def test_chat_with_usage_same_semantics(monkeypatch):
    p = _provider(monkeypatch, _resp(_msg(content="", reasoning="答案A")))
    text, usage = await p.chat_with_usage([{"role": "user", "content": "q"}])
    assert text == "答案A" and usage == {"input": 10, "output": 5}


async def test_tools_reasoning_fallback(monkeypatch):
    p = _provider(monkeypatch, _resp(_msg(content="", reasoning="工具答案")))
    r = await p.chat_with_tools([{"role": "user", "content": "q"}], tools=[])
    assert r["content"] == "工具答案" and r["tool_calls"] is None


async def test_stream_content_normal(monkeypatch):
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=_msg(content="你"))]),
        SimpleNamespace(choices=[SimpleNamespace(delta=_msg(content="好"))]),
    ]
    p = DeepSeekLLM()
    monkeypatch.setattr(p, "client", _fake_stream(chunks))
    out = "".join([c async for c in p.stream([{"role": "user", "content": "q"}])])
    assert out == "你好"


async def test_stream_reasoning_fallback_when_no_content(monkeypatch):
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=_msg(reasoning="先想"))]),
        SimpleNamespace(choices=[SimpleNamespace(delta=_msg(reasoning="后想"))]),
    ]
    p = DeepSeekLLM()
    monkeypatch.setattr(p, "client", _fake_stream(chunks))
    out = "".join([c async for c in p.stream([{"role": "user", "content": "q"}])])
    assert out == "先想后想"


class _FakeStreamCompletions:
    def __init__(self, chunks):
        self._chunks = chunks

    async def create(self, **kw):
        return _AsyncIter(self._chunks)


class _AsyncIter:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        self._i = 0
        return self

    async def __anext__(self):
        if self._i >= len(self._items):
            raise StopAsyncIteration
        v = self._items[self._i]
        self._i += 1
        return v


def _fake_stream(chunks):
    return SimpleNamespace(chat=SimpleNamespace(completions=_FakeStreamCompletions(chunks)))


async def test_reasoning_effort_kw_default_and_off(monkeypatch):
    from app.config import settings
    from app.providers.llm import deepseek_llm as mod
    monkeypatch.setattr(settings, "DEEPSEEK_REASONING_EFFORT", "low", raising=False)
    assert mod._reasoning_kw() == {"reasoning_effort": "low"}
    monkeypatch.setattr(settings, "DEEPSEEK_REASONING_EFFORT", "", raising=False)
    assert mod._reasoning_kw() == {}  # 空串=不下发


def test_extract_content_pure_unit():
    assert _extract_content(_msg(content="a")) == "a"
    assert _extract_content(_msg(content="", reasoning="r")) == "r"
    assert _extract_content(_msg(content="  ", reasoning="r")) == "r"  # 空白视为空
    assert _extract_content(_msg()) == ""
