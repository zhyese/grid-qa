"""Provider chat_with_tools 单测（function-calling 解析）。"""
import asyncio
from types import SimpleNamespace
import pytest
from app.providers.llm.deepseek_llm import DeepSeekLLM

pytestmark = pytest.mark.integration  # 依赖 LLM API key，CI 无 secret 跳过


def _make_resp(content, tool_calls=None):
    """构造 openai 风格响应。tool_calls: [(id, name, arguments_json_str), ...] 或 None"""
    tcs = None
    if tool_calls:
        tcs = [SimpleNamespace(id=t[0], function=SimpleNamespace(name=t[1], arguments=t[2]))
               for t in tool_calls]
    msg = SimpleNamespace(content=content, tool_calls=tcs)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_chat_with_tools_parses_tool_calls(monkeypatch):
    p = DeepSeekLLM()

    async def fake_create(**kw):
        assert "tools" in kw and "tool_choice" in kw
        return _make_resp("我先查规程", [("call_1", "search_regulation", '{"query":"主变油温高"}')])

    monkeypatch.setattr(p.client.chat.completions, "create", fake_create)
    r = asyncio.run(p.chat_with_tools(
        [{"role": "user", "content": "x"}],
        [{"type": "function", "function": {"name": "search_regulation"}}],
    ))
    assert r["content"] == "我先查规程"
    assert r["tool_calls"] == [{"id": "call_1", "name": "search_regulation", "arguments": {"query": "主变油温高"}}]


def test_chat_with_tools_no_tool_calls(monkeypatch):
    p = DeepSeekLLM()

    async def fake_create(**kw):
        return _make_resp("最终诊断：...", None)

    monkeypatch.setattr(p.client.chat.completions, "create", fake_create)
    r = asyncio.run(p.chat_with_tools([{"role": "user", "content": "x"}], []))
    assert r["tool_calls"] is None
    assert r["content"].startswith("最终诊断")


def test_chat_with_tools_bad_json_args(monkeypatch):
    """arguments 非法 JSON → 返回空 dict，不崩"""
    p = DeepSeekLLM()

    async def fake_create(**kw):
        return _make_resp("", [("call_2", "search_regulation", "不是json")])

    monkeypatch.setattr(p.client.chat.completions, "create", fake_create)
    r = asyncio.run(p.chat_with_tools([{"role": "user", "content": "x"}], []))
    assert r["tool_calls"][0]["arguments"] == {}
