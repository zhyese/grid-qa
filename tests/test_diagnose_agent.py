"""Agentic 诊断单测：工具层 / agent 循环 / 降级。"""
import asyncio
from app.services import diagnose_agent_service as svc


# ---------- 工具层 ----------
def test_tool_search_regulation(monkeypatch):
    async def fake_mixed(db, query, topk, **kw):
        return [{"chunk": "油温超 85℃ 应检查冷却系统与负荷", "docName": "主变规程", "score": 0.9}]
    monkeypatch.setattr(svc.retrieval_service, "mixed_search", fake_mixed)
    out = asyncio.run(svc._t_search_regulation(db=None, model_type=None, query="主变油温高"))
    assert "油温超 85℃" in out and "主变规程" in out


def test_tool_search_regulation_empty(monkeypatch):
    async def fake_mixed(db, query, topk, **kw):
        return []
    monkeypatch.setattr(svc.retrieval_service, "mixed_search", fake_mixed)
    out = asyncio.run(svc._t_search_regulation(db=None, model_type=None, query="无"))
    assert "未检索到" in out


def test_tool_query_equipment_graph(monkeypatch):
    async def fake_ctx(query, topk=8):
        return ["主变压器 --发生--> 风扇故障", "风扇故障 --表现为--> 过热"]
    monkeypatch.setattr(svc.kg_service, "graph_context", fake_ctx)
    out = asyncio.run(svc._t_query_equipment_graph(db=None, model_type=None, entity="主变压器"))
    assert "风扇故障" in out


def test_tool_search_similar_case(monkeypatch):
    async def fake_case(db, symptom, model_type, topk):
        return {"cases": [{"docName": "某站案例", "text": "曾发生风扇停转致过热", "score": 0.8}]}
    monkeypatch.setattr(svc.domain_service, "similar_case", fake_case)
    out = asyncio.run(svc._t_search_similar_case(db=None, model_type=None, symptom="主变过热"))
    assert "风扇停转" in out


def test_tool_draft_ticket(monkeypatch):
    async def fake_ticket(db, task, model_type, topk):
        return {"ticket": {"device": "1号主变", "steps": ["停电", "验电"],
                           "safety": ["戴绝缘手套"], "risks": ["触电"]}}
    monkeypatch.setattr(svc.domain_service, "generate_ticket", fake_ticket)
    out = asyncio.run(svc._t_draft_ticket(db=None, model_type=None, task="1号主变转检修"))
    assert "1号主变" in out and "停电" in out


def test_run_tool_dispatch_unknown():
    out = asyncio.run(svc._run_tool(None, None, "no_such_tool", {}))
    assert "未知工具" in out


def test_run_tool_handles_handler_error(monkeypatch):
    async def boom(db, model_type, **a):
        raise RuntimeError("下游挂了")
    monkeypatch.setattr(svc, "_t_search_regulation", boom)
    monkeypatch.setitem(svc._HANDLERS, "search_regulation", boom)
    out = asyncio.run(svc._run_tool(None, None, "search_regulation", {"query": "x"}))
    assert "执行失败" in out and "下游挂了" in out


# ---------- agent 循环 ----------
class _ScriptedProvider:
    """按脚本依次返回 chat_with_tools 响应。"""
    def __init__(self, script):
        self.script = list(script)   # [(content, tool_calls|None), ...]

    async def chat_with_tools(self, messages, tools, **kw):
        content, tcs = self.script.pop(0)
        return {"content": content, "tool_calls": tcs}


def _tc(id_, name, **args):
    return {"id": id_, "name": name, "arguments": args}


def test_agent_loop_happy_path(monkeypatch):
    # 第1轮调检索，第2轮调图谱，第3轮 final
    prov = _ScriptedProvider([
        ("先查规程", [_tc("1", "search_regulation", query="主变油温高")]),
        ("再查图谱", [_tc("2", "query_equipment_graph", entity="1号主变")]),
        ('{"causes":[{"name":"冷却系统故障","likelihood":"高","evidence":"风扇故障","handling":"检查风扇"}],"summary":"冷却不足致过热","risks":["高温跳闸"]}', None),
    ])
    monkeypatch.setattr(svc, "get_llm_provider", lambda mt=None: prov)

    async def fake_run_tool(db, mt, name, args):
        return f"{name} 结果摘要"
    monkeypatch.setattr(svc, "_run_tool", fake_run_tool)

    r = asyncio.run(svc.diagnose_agent(db=None, symptom="1号主变油温高", model_type=None))
    assert r["degraded"] is False
    assert r["iterations"] == 3                       # 2 个工具步 + 1 个收尾步
    assert [s["tool"] for s in r["steps"]] == ["search_regulation", "query_equipment_graph", None]
    assert r["diagnosis"]["summary"] == "冷却不足致过热"
    assert r["diagnosis"]["causes"][0]["name"] == "冷却系统故障"


def test_agent_loop_degrades_on_max_iter(monkeypatch):
    # 永远要调工具 → 触发 MAX_ITER → 降级
    prov = _ScriptedProvider([("继续查", [_tc(str(i), "search_regulation", query="x")]) for i in range(99)])
    monkeypatch.setattr(svc, "get_llm_provider", lambda mt=None: prov)

    async def fake_run_tool(db, mt, name, args):
        return "ok"
    monkeypatch.setattr(svc, "_run_tool", fake_run_tool)

    fallback_called = []

    async def fake_diagnose(db, symptom, model_type=None, topk=5):
        fallback_called.append(symptom)
        return {"diagnosis": {"summary": "兜底", "causes": []}}
    monkeypatch.setattr(svc.domain_service, "diagnose", fake_diagnose)

    r = asyncio.run(svc.diagnose_agent(db=None, symptom="循环症状", model_type=None))
    assert r["degraded"] is True
    assert r["degradeReason"] == "max_iter"
    assert fallback_called == ["循环症状"]


def test_agent_loop_degrades_on_exception(monkeypatch):
    class _Boom:
        async def chat_with_tools(self, *a, **kw):
            raise RuntimeError("LLM 挂了")
    monkeypatch.setattr(svc, "get_llm_provider", lambda mt=None: _Boom())

    async def fake_diagnose(db, symptom, model_type=None, topk=5):
        return {"diagnosis": {"summary": "兜底", "causes": []}}
    monkeypatch.setattr(svc.domain_service, "diagnose", fake_diagnose)

    r = asyncio.run(svc.diagnose_agent(db=None, symptom="x", model_type=None))
    assert r["degraded"] is True
    assert "exception" in r["degradeReason"]
