"""可视化工作流编排测试。

覆盖：图校验（环/孤儿/条件出边标注意大利面防护）/ 拓扑序 / 变量插值 /
引擎执行（检索→LLM→输出 快乐路径、condition 分支与 skipped、节点失败隔离）/
服务 CRUD（重名/校验/租户隔离/版本号）/ start_run 落行与调度。
全部 sqlite in-memory，CI 安全（不碰 Milvus/LLM/真 MySQL）。
"""
import asyncio

import pytest

from app.core.response import BizError
from app.services import workflow_engine as we
from app.services import workflow_service as ws


# ===== 辅助 =====

def node(id_, type_, key=None, params=None):
    return {"id": id_, "type": type_, "key": key or id_, "label": id_,
            "params": params or {}, "x": 0, "y": 0}


def edge(s, t, branch=None):
    return {"id": f"{s}-{t}", "source": s, "target": t, **({"branch": branch} if branch else {})}


def simple_graph():
    """input → llm → output 最小合法图。"""
    return {"nodes": [
        node("in", "input"),
        node("llm1", "llm", key="gen", params={"prompt": "问：{{input.query}}"}),
        node("out", "output"),
    ], "edges": [edge("in", "llm1"), edge("llm1", "out")]}


# ===== validate_graph =====

def test_validate_empty_graph():
    assert we.validate_graph({"nodes": [], "edges": []}) == ["图中没有节点"]
    assert we.validate_graph({}) == ["图中没有节点"]


def test_validate_minimal_ok():
    assert we.validate_graph(simple_graph()) == []


def test_validate_requires_single_input_and_output():
    g = simple_graph()
    g["nodes"].append(node("in2", "input"))
    errs = we.validate_graph(g)
    assert any("输入节点" in e for e in errs)
    g2 = {"nodes": [node("in", "input"), node("llm1", "llm", params={"prompt": "x"})], "edges": [edge("in", "llm1")]}
    assert any("输出节点" in e for e in we.validate_graph(g2))


def test_validate_detects_cycle():
    g = {"nodes": [
        node("in", "input"), node("a", "llm", params={"prompt": "x"}),
        node("b", "llm", params={"prompt": "x"}), node("out", "output"),
    ], "edges": [edge("in", "a"), edge("a", "b"), edge("b", "a"), edge("a", "out")]}
    errs = we.validate_graph(g)
    assert any("环或不可达" in e for e in errs)


def test_validate_edge_refs_and_duplicate_key():
    g = simple_graph()
    g["edges"].append(edge("in", "ghost"))
    assert any("不存在" in e for e in we.validate_graph(g))
    g2 = simple_graph()
    g2["nodes"][1]["key"] = "input"  # 与 input 节点 key 撞
    assert any("key" in e for e in we.validate_graph(g2))


def test_validate_condition_edges_must_be_labeled():
    g = {"nodes": [
        node("in", "input"),
        node("cond", "condition", params={"op": "contains", "keyword": "跳闸"}),
        node("out", "output"),
    ], "edges": [edge("in", "cond"), edge("cond", "out")]}
    errs = we.validate_graph(g)
    assert any("branch" in e for e in errs)
    g["edges"][1]["branch"] = "true"
    assert we.validate_graph(g) == []


def test_validate_param_rules():
    g = simple_graph()
    g["nodes"][1]["params"] = {}
    assert any("prompt" in e for e in we.validate_graph(g))
    g2 = simple_graph()
    g2["nodes"][1] = node("ag", "agent", params={"message": "x"})  # 缺 persona
    assert any("persona" in e for e in we.validate_graph(g2))


# ===== topo_order / interpolate =====

def test_topo_parents_before_children():
    g = simple_graph()
    order = we.topo_order(g["nodes"], g["edges"])
    assert order.index("in") < order.index("llm1") < order.index("out")


def test_interpolate_basic_and_missing():
    ctx = {"input": {"query": "主变油温高"}, "gen": {"text": "答案A"}}
    out, warns = we.interpolate("问：{{input.query}} 答：{{gen.text}}", ctx)
    assert out == "问：主变油温高 答：答案A"
    assert warns == []
    out2, warns2 = we.interpolate("{{nope.field}}", ctx)
    assert out2 == "" and len(warns2) == 1


def test_stringify_prefers_primary_fields():
    assert we._stringify({"text": "t", "answer": "a"}) == "t"
    assert we._stringify({"query": "q"}) == "q"
    assert we._stringify(None) == ""


# ===== execute_graph =====

@pytest.mark.asyncio
async def test_execute_happy_path(monkeypatch):
    async def fake_mixed_search(db, query, topk, **kw):
        return [{"docName": "规程", "chunk": "油温上限85度"}]
    monkeypatch.setattr("app.services.retrieval_service.mixed_search", fake_mixed_search)

    class FakeProvider:
        async def chat(self, messages, **kw):
            return "答复：" + messages[-1]["content"]
    monkeypatch.setattr("app.providers.factory.get_llm_provider", lambda m=None: FakeProvider())

    g = {"nodes": [
        node("in", "input"),
        node("r", "retrieval", key="ret", params={"topk": 3}),
        node("llm1", "llm", key="gen", params={"prompt": "依据：{{ret.context}}"}),
        node("out", "output"),
    ], "edges": [edge("in", "r"), edge("r", "llm1"), edge("llm1", "out")]}
    res = await we.execute_graph(None, g, {"query": "油温"}, {"tenant": "t1"})
    assert res["status"] == "done"
    assert res["output"] == "答复：依据：[1] 规程: 油温上限85度"
    assert all(s["status"] == "done" for s in res["node_states"])
    assert not res["error"]


@pytest.mark.asyncio
async def test_execute_condition_branch_and_skip(monkeypatch):
    class FakeProvider:
        async def chat(self, messages, **kw):
            return "需停电处置" if "停电" in messages[-1]["content"] else "常规巡视"
    monkeypatch.setattr("app.providers.factory.get_llm_provider", lambda m=None: FakeProvider())

    def _graph():
        return {"nodes": [
            node("in", "input"),
            node("llm1", "llm", key="gen", params={"prompt": "{{input.query}}"}),
            node("cond", "condition", key="cond",
                 params={"op": "contains", "keyword": "停电"}),
            node("outA", "output"), node("outB", "output"),
        ], "edges": [edge("in", "llm1"), edge("llm1", "cond"),
                     edge("cond", "outA", branch="true"), edge("cond", "outB", branch="false")]}

    res = await we.execute_graph(None, _graph(), {"query": "故障含停电风险"}, {"tenant": "t1"})
    st = {s["key"]: s["status"] for s in res["node_states"]}
    assert st["outA"] == "done" and st["outB"] == "skipped"
    assert res["output"] == "需停电处置"

    res2 = await we.execute_graph(None, _graph(), {"query": "正常巡检"}, {"tenant": "t1"})
    st2 = {s["key"]: s["status"] for s in res2["node_states"]}
    assert st2["outA"] == "skipped" and st2["outB"] == "done"


@pytest.mark.asyncio
async def test_execute_node_failure_isolates_branch(monkeypatch):
    async def boom(db, query, topk, **kw):
        raise RuntimeError("milvus 炸了")
    monkeypatch.setattr("app.services.retrieval_service.mixed_search", boom)

    g = {"nodes": [
        node("in", "input"),
        node("r", "retrieval", key="ret"),
        node("out", "output"),
    ], "edges": [edge("in", "r"), edge("r", "out")]}
    res = await we.execute_graph(None, g, {"query": "q"}, {"tenant": "t1"})
    st = {s["key"]: s["status"] for s in res["node_states"]}
    assert st["ret"] == "error" and st["out"] == "skipped"
    assert res["status"] == "failed" and "milvus" in res["error"]


@pytest.mark.asyncio
async def test_execute_agent_node_with_persona(monkeypatch):
    class FakePersona:
        name = "qa"
        system_prompt = "s"
        allowed_tools = []
        max_iter = 1
        temperature = 0.2
        max_tokens = 10
        output_format = "text"

    class FakeResult:
        answer = "agent 答案"
        steps = []
        iterations = 1
        degraded = False
        degrade_reason = None
        tools_used = []

    async def fake_get_persona(name):
        assert name == "qa"
        return FakePersona()

    async def fake_run_agent(db, persona, user_msg, **kw):
        return FakeResult()

    monkeypatch.setattr("app.services.persona_store.get_persona", fake_get_persona)
    monkeypatch.setattr("app.services.agent_runtime.run_agent", fake_run_agent)

    g = {"nodes": [
        node("in", "input"),
        node("ag", "agent", key="ag", params={"persona": "qa"}),
        node("out", "output"),
    ], "edges": [edge("in", "ag"), edge("ag", "out")]}
    res = await we.execute_graph(None, g, {"query": "q"}, {"tenant": "t1"})
    assert res["status"] == "done" and res["output"] == "agent 答案"


@pytest.mark.asyncio
async def test_execute_agent_unknown_persona_fails_node(monkeypatch):
    async def none_persona(name):
        return None
    monkeypatch.setattr("app.services.persona_store.get_persona", none_persona)
    g = {"nodes": [
        node("in", "input"),
        node("ag", "agent", key="ag", params={"persona": "ghost"}),
        node("out", "output"),
    ], "edges": [edge("in", "ag"), edge("ag", "out")]}
    res = await we.execute_graph(None, g, {"query": "q"}, {"tenant": "t1"})
    assert res["status"] == "failed" and "persona" in res["error"]


# ===== 服务 CRUD（sqlite test_db） =====

@pytest.mark.asyncio
async def test_create_and_get_workflow(test_db):
    row = await ws.create_workflow(test_db, "巡检流", "描述", simple_graph(), "t1", "alice")
    assert row["version"] == 1
    wf = await ws.get_workflow(test_db, row["id"], "t1")
    assert wf.name == "巡检流"
    with pytest.raises(BizError):
        await ws.get_workflow(test_db, row["id"], "other-tenant")


@pytest.mark.asyncio
async def test_create_duplicate_name_rejected(test_db):
    await ws.create_workflow(test_db, "重名", "", simple_graph(), "t1", "a")
    with pytest.raises(BizError):
        await ws.create_workflow(test_db, "重名", "", simple_graph(), "t1", "a")
    # 不同租户同名不冲突（租户隔离）
    await ws.create_workflow(test_db, "重名", "", simple_graph(), "t2", "a")


@pytest.mark.asyncio
async def test_create_invalid_graph_rejected(test_db):
    bad = {"nodes": [node("in", "input")], "edges": []}  # 缺 output
    with pytest.raises(BizError):
        await ws.create_workflow(test_db, "坏图", "", bad, "t1", "a")


@pytest.mark.asyncio
async def test_update_bumps_version_and_delete(test_db):
    row = await ws.create_workflow(test_db, "v流", "", simple_graph(), "t1", "a")
    upd = await ws.update_workflow(test_db, row["id"], "t1",
                                   graph=simple_graph(), enabled=False)
    assert upd["version"] == 2 and upd["enabled"] is False
    with pytest.raises(BizError):
        await ws.update_workflow(test_db, row["id"], "t1", graph={"nodes": [], "edges": []})
    assert (await ws.delete_workflow(test_db, row["id"], "t1"))["id"] == row["id"]
    with pytest.raises(BizError):
        await ws.get_workflow(test_db, row["id"], "t1")


@pytest.mark.asyncio
async def test_start_run_creates_running_row(test_db, monkeypatch):
    scheduled = []

    async def fake_execute_run(run_id):
        scheduled.append(run_id)

    monkeypatch.setattr(ws, "execute_run", fake_execute_run)
    row = await ws.create_workflow(test_db, "运行流", "", simple_graph(), "t1", "a")
    wf = await ws.get_workflow(test_db, row["id"], "t1")
    run = await ws.start_run(test_db, wf, "主变油温高", {"k": "v"}, "t1", "alice", "editor")
    assert run["status"] == "running" and run["createdBy"] == "alice"
    assert run["input"] == {"query": "主变油温高", "vars": {"k": "v"}}
    await asyncio.sleep(0)  # create_task 调度后需让出事件循环，fake 才会执行
    assert scheduled == [run["id"]]
    with pytest.raises(BizError):
        await ws.start_run(test_db, wf, "  ", None, "t1", "alice", "editor")


@pytest.mark.asyncio
async def test_runs_list_and_get_scoped_by_tenant(test_db, monkeypatch):
    async def noop(run_id):
        return None
    monkeypatch.setattr(ws, "execute_run", noop)
    row = await ws.create_workflow(test_db, "历史流", "", simple_graph(), "t1", "a")
    wf = await ws.get_workflow(test_db, row["id"], "t1")
    run = await ws.start_run(test_db, wf, "q1", None, "t1", "a", "editor")
    total, items = await ws.list_runs(test_db, row["id"], "t1")
    assert total == 1 and items[0]["id"] == run["id"]
    detail = await ws.get_run(test_db, run["id"], "t1")
    assert detail["output"] == "" and detail["status"] == "running"
    with pytest.raises(BizError):
        await ws.get_run(test_db, run["id"], "t2")  # 跨租户不可见
