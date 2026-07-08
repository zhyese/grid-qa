"""通用 Agent 引擎单测。异步用 asyncio.run（项目无 pytest-asyncio）。"""
import asyncio

import pytest

from app.services.agent_runtime import (
    AgentResult, Persona, Tool, ToolRegistry, _extract_json, _to_openai_tool_calls,
)


def test_tool_schema_is_openai_function_format():
    t = Tool(name="foo", description="do foo", parameters={"type": "object"}, handler=lambda *a, **k: None)
    assert t.schema == {"type": "function", "function": {
        "name": "foo", "description": "do foo", "parameters": {"type": "object"}}}


def test_tool_registry_run_unknown_tool_returns_error_flag():
    reg = ToolRegistry()
    result, err = asyncio.run(reg.run(db=None, model_type=None, name="nope", args={}))
    assert err is True
    assert "未知工具" in result


def test_tool_registry_run_isolates_handler_exception():
    async def boom(db, model_type, **args):
        raise RuntimeError("kaboom")
    reg = ToolRegistry()
    reg.register(Tool("boom", "d", {}, boom))
    result, err = asyncio.run(reg.run(None, None, "boom", {}))
    assert err is True
    assert "执行失败" in result


def test_tool_registry_schemas_for_returns_subset():
    async def h(db, mt, **a):
        return "ok"
    reg = ToolRegistry()
    reg.register(Tool("a", "da", {}, h))
    reg.register(Tool("b", "db", {}, h))
    schemas = reg.schemas_for(["a", "missing", "b"])
    assert [s["function"]["name"] for s in schemas] == ["a", "b"]


def test_extract_json_parses_embedded_json():
    assert _extract_json('噪声 {"a": 1} 尾巴') == {"a": 1}
    assert _extract_json("无 json") is None


def test_to_openai_tool_calls_serializes_arguments():
    out = _to_openai_tool_calls([{"id": "x", "name": "foo", "arguments": {"q": "中文"}}])
    assert out == [{"id": "x", "type": "function",
                    "function": {"name": "foo", "arguments": '{"q": "中文"}'}}]


def test_persona_defaults():
    p = Persona(name="qa", system_prompt="s", allowed_tools=[])
    assert p.max_iter == 6 and p.temperature == 0.2 and p.output_format == "text"
    assert p.config_source == "code"
