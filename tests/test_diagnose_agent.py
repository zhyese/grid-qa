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
