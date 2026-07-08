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


# ===== Task 2: agent_tools 工具包装 =====
from app.services import agent_tools


def test_search_regulation_wraps_mixed_search(monkeypatch):
    async def fake_mixed(db, q, topk, model_type=None):
        return [{"docName": "手册A", "chunk": "油温超过95度应..."}]
    monkeypatch.setattr(agent_tools.retrieval_service, "mixed_search", fake_mixed)
    out = asyncio.run(agent_tools._t_search_regulation(db=None, model_type=None, query="油温高"))
    assert "手册A" in out and "油温" in out


def test_query_equipment_graph_empty_returns_hint(monkeypatch):
    async def fake_graph(entity, limit):
        return []
    monkeypatch.setattr(agent_tools.kg_service, "graph_context", fake_graph)
    out = asyncio.run(agent_tools._t_query_equipment_graph(None, None, entity="1号主变"))
    assert "无" in out


def test_search_similar_case_wraps_domain(monkeypatch):
    async def fake_case(db, symptom, mt, topk):
        return {"cases": [{"docName": "案例X", "text": "历史上风扇故障..."}]}
    monkeypatch.setattr(agent_tools.domain_service, "similar_case", fake_case)
    out = asyncio.run(agent_tools._t_search_similar_case(None, None, symptom="过热"))
    assert "案例X" in out


def test_draft_ticket_wraps_domain(monkeypatch):
    async def fake_ticket(db, task, mt, topk):
        return {"ticket": {"device": "1号主变", "steps": ["断开开关"], "safety": ["验电"], "risks": []}}
    monkeypatch.setattr(agent_tools.domain_service, "generate_ticket", fake_ticket)
    out = asyncio.run(agent_tools._t_draft_ticket(None, None, task="转检修"))
    assert "1号主变" in out and "断开开关" in out


def test_default_registry_has_four_tools():
    names = {t.name for t in agent_tools.DEFAULT_REGISTRY._tools.values()}
    assert names == {"search_regulation", "query_equipment_graph", "search_similar_case", "draft_ticket"}
