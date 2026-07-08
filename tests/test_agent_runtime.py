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


# ===== Task 3: metrics 预注册 =====
from app.core import metrics
from prometheus_client import generate_latest


def test_agent_metrics_preregistered_in_registry():
    metrics.init_metric_series()
    text = generate_latest().decode("utf-8")
    assert "grid_agent_calls_total" in text
    assert 'persona="diagnose"' in text
    assert "grid_agent_tool_calls_total" in text
    for t in ("search_regulation", "query_equipment_graph", "search_similar_case", "draft_ticket"):
        assert f'persona="diagnose",tool="{t}"' in text


# ===== Task 4: run_agent 引擎循环 =====
from app.services import agent_runtime
from app.services.agent_runtime import Persona, Tool, ToolRegistry, run_agent


class FakeProvider:
    """脚本化 chat_with_tools：按顺序返回预设响应。"""
    def __init__(self, script):
        self.script = script
        self.i = 0
        self.calls = 0

    async def chat_with_tools(self, messages, tools, tool_choice="auto",
                              temperature=0.2, max_tokens=2048, **kw):
        self.calls += 1
        resp = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return {"content": resp.get("content"), "tool_calls": resp.get("tool_calls")}


def _reg_with(tool_name, handler):
    reg = ToolRegistry()
    reg.register(Tool(tool_name, "d", {"type": "object"}, handler))
    return reg


def test_run_agent_normal_path_breaks_when_no_tool_calls(monkeypatch):
    async def h(db, mt, **a):
        return "证据:xxx"
    persona = Persona(name="qa", system_prompt="s", allowed_tools=["h1"], output_format="text")
    fake = FakeProvider([
        {"content": "查一下", "tool_calls": [{"id": "1", "name": "h1", "arguments": {}}]},
        {"content": "最终答案", "tool_calls": None},
    ])
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda mt: fake)
    res = asyncio.run(run_agent(db=None, persona=persona, user_msg="q",
                                registry=_reg_with("h1", h)))
    assert res.degraded is False and res.iterations == 2
    assert res.answer == "最终答案"
    assert res.tools_used == ["h1"]
    assert res.steps[0]["tool"] == "h1" and res.steps[0]["error"] is False
    assert res.steps[1]["tool"] is None  # 收尾思考步


def test_run_agent_json_output_format_extracts(monkeypatch):
    async def h(db, mt, **a):
        return "e"
    persona = Persona(name="diagnose", system_prompt="s", allowed_tools=["h1"], output_format="json")
    fake = FakeProvider([{"content": '前缀 {"causes":[],"summary":"ok"}', "tool_calls": None}])
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda mt: fake)
    res = asyncio.run(run_agent(None, persona, "q", registry=_reg_with("h1", h)))
    assert res.answer == {"causes": [], "summary": "ok"}


def test_run_agent_max_iter_degrades_to_fallback(monkeypatch):
    async def h(db, mt, **a):
        return "e"
    async def fb(db, msg, mt):
        return {"summary": "降级结果"}
    persona = Persona(name="qa", system_prompt="s", allowed_tools=["h1"],
                      max_iter=2, fallback=fb)
    # 每轮都返回 tool_calls → 永不 break → 超限降级
    fake = FakeProvider([{"content": "继续", "tool_calls": [{"id": "1", "name": "h1", "arguments": {}}]}])
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda mt: fake)
    res = asyncio.run(run_agent(None, persona, "q", registry=_reg_with("h1", h)))
    assert res.degraded is True and res.degrade_reason == "max_iter"
    assert res.answer == {"summary": "降级结果"}


def test_run_agent_provider_exception_degrades(monkeypatch):
    class Boom:
        async def chat_with_tools(self, *a, **k):
            raise RuntimeError("net")
    async def fb(db, msg, mt):
        return {"summary": "fb"}
    persona = Persona(name="qa", system_prompt="s", allowed_tools=[], fallback=fb)
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda mt: Boom())
    res = asyncio.run(run_agent(None, persona, "q", registry=ToolRegistry()))
    assert res.degraded is True and "exception" in res.degrade_reason


def test_run_agent_per_tool_error_isolated(monkeypatch):
    async def boom(db, mt, **a):
        raise ValueError("bad")
    persona = Persona(name="qa", system_prompt="s", allowed_tools=["h1"])
    fake = FakeProvider([
        {"content": "c", "tool_calls": [{"id": "1", "name": "h1", "arguments": {}}]},
        {"content": "final", "tool_calls": None},
    ])
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda mt: fake)
    res = asyncio.run(run_agent(None, persona, "q", registry=_reg_with("h1", boom)))
    assert res.degraded is False  # 工具失败不崩循环
    assert res.steps[0]["error"] is True and "执行失败" in res.steps[0]["result"]


# ===== Task 5: DIAGNOSE_PERSONA =====
from app.services import agent_personas


def test_diagnose_persona_config():
    p = agent_personas.DIAGNOSE_PERSONA
    assert p.name == "diagnose"
    assert p.output_format == "json"
    assert p.max_iter == 6
    assert set(p.allowed_tools) == {"search_regulation", "query_equipment_graph",
                                    "search_similar_case", "draft_ticket"}
    assert "电网运维" in p.system_prompt or "诊断" in p.system_prompt


def test_diagnose_fallback_strips_prefix_and_calls_domain(monkeypatch):
    captured = {}
    async def fake_diagnose(db, symptom, mt):
        captured["symptom"] = symptom
        return {"diagnosis": {"summary": "s", "causes": []}}
    monkeypatch.setattr(agent_personas.domain_service, "diagnose", fake_diagnose)
    out = asyncio.run(agent_personas._diagnose_fallback(
        db=None, user_msg="故障症状：1号主变油温高", model_type=None))
    assert captured["symptom"] == "1号主变油温高"  # 前缀已剥离
    assert out == {"summary": "s", "causes": []}


# ===== Task 6: 迁移不变性黄金回归 =====
from app.services import diagnose_agent_service


def test_diagnose_agent_migration_returns_stable_schema(monkeypatch):
    """黄金回归：迁移后 diagnose_agent 返回 schema 与原实现一致；mock 工具下游避免真实 DB。"""
    async def fake_mixed(db, q, topk, model_type=None):
        return [{"docName": "规程A", "chunk": "油温高处置..."}]
    async def fake_graph(entity, limit):
        return ["风扇故障→过热"]
    async def fake_case(db, symptom, mt, topk):
        return {"cases": [{"docName": "案例B", "text": "历史风扇故障"}]}
    async def fake_ticket(db, task, mt, topk):
        return {"ticket": {"device": "1号主变", "steps": ["停机"], "safety": [], "risks": []}}
    monkeypatch.setattr(agent_tools.retrieval_service, "mixed_search", fake_mixed)
    monkeypatch.setattr(agent_tools.kg_service, "graph_context", fake_graph)
    monkeypatch.setattr(agent_tools.domain_service, "similar_case", fake_case)
    monkeypatch.setattr(agent_tools.domain_service, "generate_ticket", fake_ticket)

    fake = FakeProvider([
        {"content": "查规程", "tool_calls": [
            {"id": "1", "name": "search_regulation", "arguments": {"query": "油温高"}}]},
        {"content": "查图谱", "tool_calls": [
            {"id": "2", "name": "query_equipment_graph", "arguments": {"entity": "1号主变"}}]},
        {"content": '{"causes":[{"name":"风扇故障","likelihood":"高","evidence":"e","handling":"h"}],'
                    '"summary":"过热","risks":["负载高"]}', "tool_calls": None},
    ])
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda mt: fake)
    out = asyncio.run(diagnose_agent_service.diagnose_agent(
        db=None, symptom="1号主变油温高", model_type=None))
    assert set(out.keys()) == {"symptom", "diagnosis", "steps", "iterations",
                               "degraded", "degradeReason", "latencyMs"}
    assert out["symptom"] == "1号主变油温高"
    assert out["degraded"] is False and out["degradeReason"] is None
    assert out["iterations"] == 3
    assert out["diagnosis"]["summary"] == "过热"
    assert out["diagnosis"]["causes"][0]["name"] == "风扇故障"
    assert [s["tool"] for s in out["steps"]] == ["search_regulation", "query_equipment_graph", None]


def test_endpoint_smoke_diagnose_agent_route():
    """端点 smoke：路由模块可导入，/domain/diagnose-agent 路径不变。"""
    from app.routers.domain import router
    paths = {r.path for r in router.routes}
    assert "/domain/diagnose-agent" in paths


# ===== S4: 工具审计 =====
from app.models.agent_tool_call import AgentToolCall
from app.services import agent_tool_audit_service


def test_log_tool_call_writes_record(monkeypatch):
    class FakeSession:
        def __init__(self): self.records = []
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def add(self, obj): self.records.append(obj)
        async def commit(self): pass
    fake = FakeSession()
    monkeypatch.setattr(agent_tool_audit_service, "AsyncSessionLocal", lambda: fake)
    asyncio.run(agent_tool_audit_service.log_tool_call(
        persona="diagnose", tool="search_regulation", iter=1,
        args={"query": "油温高"}, result="证据摘要", error=False,
        username="alice", tenant="t1", role="user"))
    rec = fake.records[0]
    assert isinstance(rec, AgentToolCall)
    assert rec.persona == "diagnose" and rec.tool == "search_regulation"
    assert rec.username == "alice" and rec.tenant == "t1" and rec.role == "user"
    assert rec.error is False and "油温高" in rec.args_json


def test_tool_registry_permission_denied_for_non_admin():
    """S4: draft_ticket 非 admin → 拒绝，handler 不被调。"""
    reg = ToolRegistry()
    async def h(db, mt, **a): return "should not reach"
    reg.register(Tool("draft_ticket", "d", {"type": "object"}, h))
    result, err = asyncio.run(reg.run(None, None, "draft_ticket", {"task": "x"},
                                      ctx={"role": "user", "username": "bob"}))
    assert err is True
    assert "权限不足" in result


def test_tool_registry_permission_allowed_for_admin(monkeypatch):
    """S4: draft_ticket admin → 正常执行。monkeypatch 审计避免真 DB。"""
    async def _noop(*a, **k): pass
    monkeypatch.setattr("app.services.agent_tool_audit_service.log_tool_call", _noop)
    reg = ToolRegistry()
    async def h(db, mt, **a): return "ok"
    reg.register(Tool("draft_ticket", "d", {"type": "object"}, h))
    result, err = asyncio.run(reg.run(None, None, "draft_ticket", {"task": "x"},
                                      ctx={"role": "admin", "username": "alice"}))
    assert err is False and result == "ok"


def test_tool_registry_ctx_none_skips_permission_and_audit():
    """S4: ctx=None 时跳过权限+审计（diagnose 老链路零回归；draft_ticket 也直通）。"""
    reg = ToolRegistry()
    async def h(db, mt, **a): return "ok"
    reg.register(Tool("draft_ticket", "d", {"type": "object"}, h))
    result, err = asyncio.run(reg.run(None, None, "draft_ticket", {"task": "x"}))  # 无 ctx
    assert err is False and result == "ok"


def test_endpoint_smoke_agent_tool_calls_route():
    """S4: /system/agent/tool-calls 审计接口路由注册。"""
    from app.routers.system import router
    paths = {r.path for r in router.routes}
    assert "/system/agent/tool-calls" in paths


# ===== S2: 问答 Agent =====
from app.services.agent_personas import QA_PERSONA


def test_qa_persona_config():
    assert QA_PERSONA.name == "qa"
    assert QA_PERSONA.output_format == "text"
    assert "draft_ticket" not in QA_PERSONA.allowed_tools  # 问答不需要操作票
    assert "search_regulation" in QA_PERSONA.allowed_tools


def test_run_agent_on_step_callback_fires(monkeypatch):
    """S2: on_step 每步触发（工具步 + 收尾步）；默认 None 零回归。"""
    captured = []
    async def h(db, mt, **a): return "e"
    persona = Persona(name="qa", system_prompt="s", allowed_tools=["h1"], output_format="text")
    fake = FakeProvider([
        {"content": "查", "tool_calls": [{"id": "1", "name": "h1", "arguments": {}}]},
        {"content": "答案", "tool_calls": None},
    ])
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda mt: fake)
    asyncio.run(run_agent(None, persona, "q", registry=_reg_with("h1", h),
                          on_step=lambda s: captured.append(s)))
    assert len(captured) == 2  # 1 工具步 + 1 收尾步
    assert captured[0]["tool"] == "h1"
    assert captured[1]["tool"] is None


def test_stream_agent_event_sequence(monkeypatch):
    """S2: _stream_agent 事件序列 meta→tool_step→token→done。"""
    from app.services import qa_service
    from app.services.agent_runtime import AgentResult

    async def fake_run(db, persona, msg, mt, ctx=None, on_step=None, registry=None):
        if on_step:
            on_step({"iter": 1, "tool": "search_regulation", "args": {},
                     "result": "证据", "error": False})
        return AgentResult(answer="最终答案", steps=[], iterations=1, degraded=False,
                           degrade_reason=None, latency_ms=10, persona="qa",
                           tools_used=["search_regulation"])

    monkeypatch.setattr("app.services.agent_runtime.run_agent", fake_run)

    class _C: id = "c1"
    async def fake_create(db, username, query): return _C()
    async def fake_save(db, cid, role, text): pass
    monkeypatch.setattr(qa_service.conversation_service, "create_conversation", fake_create)
    monkeypatch.setattr(qa_service.conversation_service, "save_message", fake_save)

    events = []
    async def collect():
        async for ev in qa_service._stream_agent(None, "问", None, None, "u", "t", 0):
            events.append(ev)
    asyncio.run(collect())
    types = [e["type"] for e in events]
    assert types[0] == "meta" and types[-1] == "done"
    assert "tool_step" in types
    token_ev = [e for e in events if e["type"] == "token"][0]
    assert token_ev["content"] == "最终答案"


def test_qa_answer_request_has_agent_mode():
    """S2: QaAnswerRequest 支持 agentMode 字段。"""
    from app.schemas.qa import QaAnswerRequest
    req = QaAnswerRequest(query="x", agentMode=True)
    assert req.agentMode is True
    assert QaAnswerRequest(query="x").agentMode is False  # 默认关
