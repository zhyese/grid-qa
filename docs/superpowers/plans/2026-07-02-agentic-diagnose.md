# Agentic 诊断 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `POST /domain/diagnose-agent`——LLM 用 function-calling 自主调用 4 个工具（检索/图谱/案例/两票）做多轮交叉验证诊断，返回诊断结果 + 完整 `steps[]` 思考链；超限/异常降级到现有 single-pass diagnose。

**Architecture:** OpenAI function-calling agent 循环（自写 ~150 行，零新依赖）。Provider 加 `chat_with_tools()`；工具定义成注册表（`ToolDef`，MCP 可直接复用）；循环到 `MAX_ITER=6` 或 LLM 给最终答案为止；任何异常/超限 → 回退 `domain_service.diagnose()`。

**Tech Stack:** FastAPI / Pydantic v2 / openai SDK（function-calling）/ Vue 3 / pytest（异步用 `asyncio.run`，无 pytest-asyncio）

## Global Constraints

- **后端无 pytest-asyncio**：异步函数测试用同步测试函数包 `asyncio.run(...)`（项目既有测试全同步）
- 测试落 `tests/`；运行 `venv/Scripts/python.exe -m pytest tests/<file> -v`（conftest 已把 backend 加 sys.path）
- 后端运行 **不带 `--reload`**：`venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --app-dir backend`（**改后端代码时绝不热重载**——上次翻车教训；改完手动重启）
- 复用既有模式：`degraded(tag,e)` / `get_llm_provider` / `write_log` / `success` / `@limiter.limit` / `init_metric_series` 预注册
- 工具注册表 `TOOLS` 接口（name/description/params schema/async handler）刻意中立——**Spec 2 (MCP) 直接复用**
- agent 不流式（一次性 JSON，与现有 diagnose 一致）
- `MAX_ITER=6`；超限/异常 → 回退 `domain_service.diagnose()`
- **实现严格在特性分支验证通过再合 main**（不再 main 直接跑）
- 项目无 PyYAML/无 FastAPI TestClient——端点验证用 py_compile + import smoke（与现有 domain 端点一致）

## File Structure

- **Create:** `backend/app/services/diagnose_agent_service.py` — 工具注册表 + handlers + agent 循环 + 降级
- **Modify:** `backend/app/providers/base.py` — 加 `chat_with_tools` 抽象
- **Modify:** `backend/app/providers/llm/{deepseek,qwen,doubao}_llm.py` — 三家各加 `chat_with_tools` 实现
- **Modify:** `backend/app/schemas/domain.py` — 加 `DiagnoseAgentRequest`
- **Modify:** `backend/app/routers/domain.py` — 加 `POST /domain/diagnose-agent`
- **Modify:** `backend/app/core/metrics.py` — 加 `AGENT_ITERS` Histogram + init 预注册 `diagnose_agent` label
- **Modify:** `frontend/src/api/index.js` — 加 `diagnoseAgent`
- **Modify:** `frontend/src/views/Diagnose.vue` — 故障诊断 tab 加「深度诊断(Agent)」开关 + steps 思考链渲染
- **Create:** `tests/test_provider_tools.py` — provider chat_with_tools 单测
- **Create:** `tests/test_diagnose_agent.py` — 工具 + 循环 + 降级单测

---

### Task 1: Provider `chat_with_tools` 扩展

**Files:**
- Modify: `backend/app/providers/base.py`
- Modify: `backend/app/providers/llm/deepseek_llm.py`、`qwen_llm.py`、`doubao_llm.py`
- Test: `tests/test_provider_tools.py`

**Interfaces:**
- Produces: `LLMProvider.chat_with_tools(messages, tools, tool_choice="auto", temperature=0.2, max_tokens=2048, **kw) -> {"content": str|None, "tool_calls": [{"id","name","arguments":dict}] | None}`

- [ ] **Step 1: 写失败测试**

`tests/test_provider_tools.py`：
```python
"""Provider chat_with_tools 单测（function-calling 解析）。"""
import asyncio
from types import SimpleNamespace
from app.providers.llm.deepseek_llm import DeepSeekLLM


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
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_provider_tools.py -v`
Expected: FAIL（`AttributeError: 'DeepSeekLLM' object has no attribute 'chat_with_tools'`）

- [ ] **Step 3: base.py 加抽象方法**

`backend/app/providers/base.py` 在 `LLMProvider` 类内、`stream` 方法之后追加：
```python
    async def chat_with_tools(self, messages: list[dict], tools: list[dict],
                              tool_choice: str = "auto", temperature: float = 0.2,
                              max_tokens: int = 2048, **kwargs) -> dict:
        """function-calling：返回 {"content": str|None, "tool_calls": [{id,name,arguments:dict}]|None}。
        子类用 openai SDK 透传 tools=。"""
        raise NotImplementedError
```

- [ ] **Step 4: 三家 LLM 各加实现**

三家实现代码相同（仅 self.client/self.model 各自不同）。在 `deepseek_llm.py` / `qwen_llm.py` / `doubao_llm.py` 的 `stream` 方法之后各追加：
```python
    async def chat_with_tools(self, messages, tools, tool_choice="auto", temperature=0.2, max_tokens=2048, **kw):
        import json as _json
        r = await self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, tool_choice=tool_choice,
            temperature=temperature, max_tokens=max_tokens, **kw,
        )
        msg = r.choices[0].message
        tool_calls = None
        if msg.tool_calls:
            tool_calls = []
            for tc in msg.tool_calls:
                try:
                    args = _json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
        return {"content": msg.content, "tool_calls": tool_calls}
```

- [ ] **Step 5: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_provider_tools.py -v`
Expected: PASS（3 个测试全过）

- [ ] **Step 6: Commit**
```bash
git add backend/app/providers/base.py backend/app/providers/llm/deepseek_llm.py backend/app/providers/llm/qwen_llm.py backend/app/providers/llm/doubao_llm.py tests/test_provider_tools.py
git commit -m "feat(provider): chat_with_tools 支持 function-calling（agent 用）"
```

---

### Task 2: 工具注册表 + handlers + dispatch

**Files:**
- Create: `backend/app/services/diagnose_agent_service.py`
- Test: `tests/test_diagnose_agent.py`

**Interfaces:**
- Consumes: `retrieval_service.mixed_search(db, query, topk, model_type=)` / `kg_service.graph_context(query, topk=8)` / `domain_service.similar_case(db, symptom, model_type, topk)` / `domain_service.generate_ticket(db, task, model_type, topk)`
- Produces:
  - `TOOLS: list[dict]` — OpenAI function schema 清单（4 个工具）
  - `async _run_tool(db, model_type, name, args) -> str` — 分发执行，返回 LLM 可读摘要；工具失败返回错误串不抛
  - `async _t_search_regulation/_t_query_equipment_graph/_t_search_similar_case/_t_draft_ticket(db, model_type, **args) -> str`

- [ ] **Step 1: 写失败测试**

`tests/test_diagnose_agent.py`：
```python
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
    async def fake_mixed(db, query, topk, **kw): return []
    monkeypatch.setattr(svc.retrieval_service, "mixed_search", fake_mixed)
    out = asyncio.run(svc._t_search_regulation(db=None, model_type=None, query="无"))
    assert "未检索到" in out


def test_tool_query_equipment_graph(monkeypatch):
    async def fake_ctx(query, topk=8): return ["主变压器 --发生--> 风扇故障", "风扇故障 --表现为--> 过热"]
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
        return {"ticket": {"device": "1号主变", "steps": ["停电", "验电"], "safety": ["戴绝缘手套"], "risks": ["触电"]}}
    monkeypatch.setattr(svc.domain_service, "generate_ticket", fake_ticket)
    out = asyncio.run(svc._t_draft_ticket(db=None, model_type=None, task="1号主变转检修"))
    assert "1号主变" in out and "停电" in out


def test_run_tool_dispatch_unknown():
    out = asyncio.run(svc._run_tool(None, None, "no_such_tool", {}))
    assert "未知工具" in out


def test_run_tool_handles_handler_error(monkeypatch):
    async def boom(db, model_type, **a): raise RuntimeError("下游挂了")
    monkeypatch.setattr(svc, "_t_search_regulation", boom)
    monkeypatch.setitem(svc._HANDLERS, "search_regulation", boom)
    out = asyncio.run(svc._run_tool(None, None, "search_regulation", {"query": "x"}))
    assert "执行失败" in out and "下游挂了" in out
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_diagnose_agent.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.services.diagnose_agent_service'`）

- [ ] **Step 3: 实现 service 工具层**

`backend/app/services/diagnose_agent_service.py`：
```python
"""Agentic 诊断：LLM 用 function-calling 自主调用工具做多轮交叉验证诊断。

工具定义成注册表（TOOLS + _HANDLERS），接口中立——Spec 2(MCP) 直接复用包装对外。
循环到 MAX_ITER 或 LLM 给最终答案为止；超限/异常降级到 domain_service.diagnose。
"""
from app.core.obs import degraded
from app.services import retrieval_service, kg_service, domain_service

_TOPK = 5


# ---------- 工具实现（包装现有 service，返回 LLM 可读摘要）----------
async def _t_search_regulation(db, model_type, query):
    """检索运维规程/手册。"""
    ctx = await retrieval_service.mixed_search(db, query, _TOPK, model_type=model_type)
    return _fmt_chunks(ctx) or "未检索到相关规程"


async def _t_query_equipment_graph(db, model_type, entity):
    """查设备-故障-处置因果链（Neo4j 图谱）。"""
    rows = await kg_service.graph_context(entity, 8)
    return "\n".join(rows) if rows else "图谱中无该设备相关因果链"


async def _t_search_similar_case(db, model_type, symptom):
    """查历史相似故障案例。"""
    res = await domain_service.similar_case(db, symptom, model_type, _TOPK)
    return _fmt_cases(res.get("cases", [])) or "未找到相似历史案例"


async def _t_draft_ticket(db, model_type, task):
    """生成处置操作票草案。"""
    res = await domain_service.generate_ticket(db, task, model_type, _TOPK)
    return _fmt_ticket(res.get("ticket", {})) or "生成操作票草案失败"


_HANDLERS = {
    "search_regulation": _t_search_regulation,
    "query_equipment_graph": _t_query_equipment_graph,
    "search_similar_case": _t_search_similar_case,
    "draft_ticket": _t_draft_ticket,
}


async def _run_tool(db, model_type, name, args):
    """分发执行；工具失败返回错误串不抛（循环不崩）。"""
    h = _HANDLERS.get(name)
    if not h:
        return f"未知工具: {name}"
    try:
        return await h(db, model_type, **(args or {}))
    except Exception as e:
        degraded(f"agent_tool_{name}", e)
        return f"工具 {name} 执行失败: {type(e).__name__}: {e}"


# ---------- 摘要格式化 ----------
def _fmt_chunks(ctx):
    if not ctx:
        return ""
    return "\n".join(f"[{i}] {(c.get('docName') or '')}: {(c.get('chunk') or '')[:200]}"
                     for i, c in enumerate(ctx[:_TOPK], 1))


def _fmt_cases(cases):
    if not cases:
        return ""
    return "\n".join(f"[{i}] {(c.get('docName') or '')}: {(c.get('text') or '')[:200]}"
                     for i, c in enumerate(cases[:_TOPK], 1))


def _fmt_ticket(ticket):
    if not ticket:
        return ""
    steps = ticket.get("steps") or []
    return (f"设备:{ticket.get('device') or '无'}\n"
            f"步骤:{';'.join(steps[:8]) if steps else '无'}\n"
            f"安措:{';'.join(ticket.get('safety') or []) or '无'}\n"
            f"风险:{';'.join(ticket.get('risks') or []) or '无'}")
```

- [ ] **Step 4: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_diagnose_agent.py -v`
Expected: PASS（7 个工具层测试全过）

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/diagnose_agent_service.py tests/test_diagnose_agent.py
git commit -m "feat(agent): 工具注册表 + 4 工具 handler + dispatch（诊断 agent 用）"
```

---

### Task 3: Agent 循环 + 降级

**Files:**
- Modify: `backend/app/services/diagnose_agent_service.py`（追加 TOOLS schema + 循环 + 降级）
- Test: `tests/test_diagnose_agent.py`（追加循环 + 降级测试）

**Interfaces:**
- Consumes: Task 1 的 `provider.chat_with_tools` + Task 2 的 `TOOLS`/`_run_tool`
- Produces: `async diagnose_agent(db, symptom, model_type=None) -> dict`，返回 `{symptom, diagnosis:{causes,summary,risks}, steps:[{iter,thought,tool,args,result,error?}], iterations, degraded, degradeReason, latencyMs}`

- [ ] **Step 1: 写失败测试（mock provider 脚本化 + 降级）**

追加到 `tests/test_diagnose_agent.py`：
```python
# ---------- agent 循环 ----------
class _ScriptedProvider:
    """按脚本依次返回 chat_with_tools 响应。"""
    def __init__(self, script):
        self.script = list(script)   # [(content, tool_calls|None), ...]
        self.calls = 0
    async def chat_with_tools(self, messages, tools, **kw):
        self.calls += 1
        content, tcs = self.script.pop(0)
        return {"content": content, "tool_calls": tcs}


def _tc(id, name, **args):
    return {"id": id, "name": name, "arguments": args}


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
    prov = _ScriptedProvider([("继续查", [_tc(str(i), "search_regulation", query="x")]) for _ in range(99)])
    monkeypatch.setattr(svc, "get_llm_provider", lambda mt=None: prov)
    async def fake_run_tool(db, mt, name, args): return "ok"
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
        async def chat_with_tools(self, *a, **kw): raise RuntimeError("LLM 挂了")
    monkeypatch.setattr(svc, "get_llm_provider", lambda mt=None: _Boom())
    async def fake_diagnose(db, symptom, model_type=None, topk=5):
        return {"diagnosis": {"summary": "兜底", "causes": []}}
    monkeypatch.setattr(svc.domain_service, "diagnose", fake_diagnose)

    r = asyncio.run(svc.diagnose_agent(db=None, symptom="x", model_type=None))
    assert r["degraded"] is True
    assert "exception" in r["degradeReason"]
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_diagnose_agent.py -v`
Expected: FAIL（`AttributeError: module ... has no attribute 'diagnose_agent'` / `'TOOLS'`）

- [ ] **Step 3: 追加 TOOLS schema + 循环 + 降级**

追加到 `diagnose_agent_service.py` 顶部 import 区：`import json`、`import time`、`from app.providers.factory import get_llm_provider`。再在文件末尾追加：
```python
MAX_ITER = 6

# ---------- OpenAI function-calling 工具 schema（Spec 2 MCP 直接复用）----------
TOOLS = [
    {"type": "function", "function": {
        "name": "search_regulation",
        "description": "检索电网运维规程/手册/标准，获取故障处置的规程依据、限值、标准步骤。",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "检索关键词，如 '主变压器油温高 处置'"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "query_equipment_graph",
        "description": "查知识图谱中设备的故障-处置因果链（设备→故障→处置 多跳）。",
        "parameters": {"type": "object",
                       "properties": {"entity": {"type": "string", "description": "设备名，如 '1号主变'"}},
                       "required": ["entity"]}}},
    {"type": "function", "function": {
        "name": "search_similar_case",
        "description": "查历史相似故障案例（故障案例库），看历史上类似故障怎么处理的。",
        "parameters": {"type": "object",
                       "properties": {"symptom": {"type": "string", "description": "故障症状描述"}},
                       "required": ["symptom"]}}},
    {"type": "function", "function": {
        "name": "draft_ticket",
        "description": "生成处置操作票草案（步骤/安措/风险）。诊断基本明确、需要处置步骤时调用。",
        "parameters": {"type": "object",
                       "properties": {"task": {"type": "string", "description": "操作任务，如 '1号主变由运行转检修'"}},
                       "required": ["task"]}}},
]

_AGENT_SYSTEM = """你是电网运维故障诊断专家。基于故障症状，通过调用工具自主收集证据（规程/图谱/历史案例）进行多轮交叉验证后给出诊断。
规则：
1) 每次可调用 0 个或多个工具；证据充分后停止调用工具，直接输出最终诊断。
2) 最终诊断必须输出严格 JSON：{"causes":[{"name":"可能原因","likelihood":"高/中/低","evidence":"资料依据","handling":"处置措施"}],"summary":"总体判断","risks":["风险点"]}
3) 原因按可能性从高到低排序；只基于工具收集的证据，证据不足如实说明；高风险处置（停电/接地/倒闸）须在 risks 标注。"""


def _to_openai_tool_calls(tool_calls):
    """把内部 dict 形式 tool_calls 转回 openai assistant 消息需要的结构。"""
    return [{"id": tc["id"], "type": "function",
             "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False)}}
            for tc in tool_calls]


def _inc_metric(iterations):
    try:
        from app.core import metrics
        metrics.DOMAIN_CALLS.labels("diagnose_agent").inc()
        metrics.AGENT_ITERS.observe(iterations)   # Task 4 定义 AGENT_ITERS；此前为 no-op
    except Exception:
        pass


async def diagnose_agent(db, symptom, model_type=None):
    """Agentic 诊断：LLM 自主调工具多轮验证 → {diagnosis, steps[], iterations, degraded, latencyMs}。"""
    t0 = time.perf_counter()
    provider = get_llm_provider(model_type)
    messages = [
        {"role": "system", "content": _AGENT_SYSTEM},
        {"role": "user", "content": f"故障症状：{symptom}"},
    ]
    steps: list[dict] = []
    try:
        resp = None
        for i in range(1, MAX_ITER + 1):
            resp = await provider.chat_with_tools(messages, TOOLS, temperature=0.2, max_tokens=1500)
            if not resp.get("tool_calls"):
                steps.append({"iter": i, "thought": resp.get("content"), "tool": None,
                              "args": None, "result": None})
                break
            # 记 assistant 消息（含 tool_calls，供下一轮引用）
            messages.append({"role": "assistant", "content": resp.get("content") or "",
                             "tool_calls": _to_openai_tool_calls(resp["tool_calls"])})
            for tc in resp["tool_calls"]:
                result = await _run_tool(db, model_type, tc["name"], tc.get("arguments"))
                steps.append({"iter": i, "thought": resp.get("content"), "tool": tc["name"],
                              "args": tc.get("arguments"), "result": (result or "")[:600]})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        else:
            # for-else：break 未触发 → 超 MAX_ITER
            degraded("diagnose_agent_maxiter", RuntimeError(f"max_iter={MAX_ITER}"))
            return await _fallback(db, symptom, model_type, "max_iter", steps, t0)

        diagnosis = domain_service._extract_json(resp.get("content") or "") or \
            {"summary": (resp.get("content") or "")[:500], "causes": []}
    except Exception as e:
        degraded("diagnose_agent_error", e)
        return await _fallback(db, symptom, model_type, f"exception:{type(e).__name__}", steps, t0)

    iters = len(steps)
    _inc_metric(iters)
    return {"symptom": symptom, "diagnosis": diagnosis, "steps": steps, "iterations": iters,
            "degraded": False, "degradeReason": None,
            "latencyMs": int((time.perf_counter() - t0) * 1000)}


async def _fallback(db, symptom, model_type, reason, steps, t0):
    """降级：调现有 single-pass diagnose，保留已收集 steps。"""
    try:
        data = await domain_service.diagnose(db, symptom, model_type)
    except Exception as e:
        degraded("diagnose_agent_fallback", e)
        data = {"diagnosis": {"summary": "诊断生成失败，请参考已收集证据", "causes": []}}
    _inc_metric(len(steps))
    return {"symptom": symptom, "diagnosis": data.get("diagnosis", {"summary": "", "causes": []}),
            "steps": steps, "iterations": len(steps), "degraded": True, "degradeReason": reason,
            "latencyMs": int((time.perf_counter() - t0) * 1000)}
```

- [ ] **Step 4: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_diagnose_agent.py -v`
Expected: PASS（工具 7 + 循环 3 = 10 个测试全过）

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/diagnose_agent_service.py tests/test_diagnose_agent.py
git commit -m "feat(agent): diagnose_agent 循环 + max_iter/异常降级到 single-pass"
```

---

### Task 4: API 层（schema + 路由 + 指标）

**Files:**
- Modify: `backend/app/schemas/domain.py`
- Modify: `backend/app/routers/domain.py`
- Modify: `backend/app/core/metrics.py`

**Interfaces:**
- Consumes: Task 3 的 `diagnose_agent_service.diagnose_agent(db, symptom, model_type)`
- Produces: `POST /api/domain/diagnose-agent`（`@limiter.limit("6/minute")` + `get_current_user` + `write_log`）；指标 `grid_agent_iters` Histogram + `DOMAIN_CALLS.labels("diagnose_agent")` 预注册

- [ ] **Step 1: 加 schema**

`backend/app/schemas/domain.py` 末尾追加（与现有 `DiagnoseRequest` 一致风格）：
```python
class DiagnoseAgentRequest(BaseModel):
    symptom: str
    modelType: Optional[str] = None
```

- [ ] **Step 2: 加路由**

`backend/app/routers/domain.py`：
- import 行追加（与现有 import 并列）：
```python
from app.schemas.domain import DiagnoseAgentRequest, DiagnoseRequest, SimilarCaseRequest, TicketAuditRequest, TicketRequest
from app.services import diagnose_agent_service
```
（按文件实际现有 import 行调整：把 `DiagnoseAgentRequest` 加进 schemas import；新增 `from app.services import diagnose_agent_service`）
- 文件末尾追加端点：
```python
@router.post("/diagnose-agent")
@limiter.limit("6/minute")
async def diagnose_agent(
    request: Request,
    body: DiagnoseAgentRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agentic 深度诊断：LLM 自主多轮调工具（检索/图谱/案例/两票）交叉验证，返回诊断 + 思考链 steps。"""
    data = await diagnose_agent_service.diagnose_agent(db, body.symptom, body.modelType)
    await write_log(db, user.username, "深度诊断", f"症状：{body.symptom[:40]}")
    return success(data, "深度诊断完成")
```

- [ ] **Step 3: 加指标 + 预注册**

`backend/app/core/metrics.py`：
- 在 `DOMAIN_CALLS = Counter(...)` 之后追加：
```python
# Agentic 诊断：循环深度分布（观测 agent 调几轮工具）
AGENT_ITERS = Histogram("grid_agent_iters", "诊断 agent 循环深度(轮)",
                        buckets=(1, 2, 3, 4, 5, 6, float("inf")))
```
- 在 `init_metric_series()` 的 try 块内（`DOMAIN_CALLS.labels("safety_block").inc(0)` 之后）追加：
```python
        # agent 诊断调用 label（复用 DOMAIN_CALLS，预注册 0）
        DOMAIN_CALLS.labels("diagnose_agent").inc(0)
```

- [ ] **Step 4: 语法检查 + import smoke + 全量回归**

```bash
venv/Scripts/python.exe -m py_compile backend/app/schemas/domain.py backend/app/routers/domain.py backend/app/core/metrics.py backend/app/services/diagnose_agent_service.py
# metrics import smoke（AGENT_ITERS 存在 + init 不报错）
venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'backend'); from app.core import metrics; metrics.init_metric_series(); print('AGENT_ITERS ok' if hasattr(metrics,'AGENT_ITERS') else 'MISSING')"
# 全量回归（Task4 定义了 AGENT_ITERS，Task3 的 _inc_metric 此刻真正生效）
venv/Scripts/python.exe -m pytest tests/test_provider_tools.py tests/test_diagnose_agent.py -v
```
Expected: py_compile 无输出（成功）；metrics smoke 打印 `AGENT_ITERS ok`；全部测试 PASS。

- [ ] **Step 5: Commit**
```bash
git add backend/app/schemas/domain.py backend/app/routers/domain.py backend/app/core/metrics.py
git commit -m "feat(agent): POST /domain/diagnose-agent 端点（限流6/min+日志）+ AGENT_ITERS 指标"
```

---

### Task 5: 前端 — api + Diagnose.vue 深度诊断开关 + steps 思考链

**Files:**
- Modify: `frontend/src/api/index.js`
- Modify: `frontend/src/views/Diagnose.vue`

**Interfaces:**
- Consumes: Task 4 的 `POST /api/domain/diagnose-agent`，响应 `{diagnosis:{causes,summary,risks}, steps:[{iter,thought,tool,args,result}], iterations, degraded, degradeReason, latencyMs}`
- Produces: `diagnoseAgent(symptom, modelType)`；Diagnose 故障诊断 tab「🔬 深度诊断(Agent)」开关 + 可折叠思考链

- [ ] **Step 1: api 加 diagnoseAgent**

`frontend/src/api/index.js` 在 `diagnose` 行之后追加：
```js
export const diagnoseAgent = (symptom, modelType) =>
  request.post('/domain/diagnose-agent', { symptom, modelType })
```

- [ ] **Step 2: Diagnose.vue 加深度诊断开关 + steps 渲染**

先 **Read `frontend/src/views/Diagnose.vue`** 确认故障诊断 tab 的输入行/结果区结构，然后：

模板——故障诊断 tab 的输入行（`tab === 'diagnose'` 的 `.row`）内，在「开始诊断」按钮前追加开关：
```html
        <label class="agent-toggle" style="display:flex;align-items:center;gap:4px;font-size:13px;color:var(--text-muted);cursor:pointer">
          <input type="checkbox" v-model="agentMode" style="cursor:pointer" /> 🔬 深度诊断(Agent)
        </label>
```
按钮的 disabled/text 与 click 改为随 agentMode 切换：
```html
        <button class="btn btn-primary" @click="doDiagnose" :disabled="loading || !symptom.trim()">{{ loading ? (agentMode ? '深度诊断中…' : '诊断中…') : (agentMode ? '深度诊断' : '开始诊断') }}</button>
```
在诊断结果区（`<div v-if="diag" class="result">` 内，`<sources-list>` 之前）追加 steps 思考链（仅 agent 模式且有 steps 时显示）：
```html
        <div v-if="agentMode && agentSteps.length" class="agent-trace">
          <div class="src-head" @click="traceOpen = !traceOpen" style="cursor:pointer">
            🧠 Agent 思考过程（{{ agentSteps.length }} 步<span v-if="agentDegraded"> · 已降级</span>）<span class="hint">{{ traceOpen ? '▾' : '▸' }}</span>
          </div>
          <div v-show="traceOpen">
            <div class="trace-step" v-for="(s, i) in agentSteps" :key="i">
              <span class="trace-iter">{{ s.iter }}</span>
              <div class="trace-body">
                <div class="trace-thought" v-if="s.thought">{{ s.thought }}</div>
                <div class="trace-tool" v-if="s.tool">🔧 {{ s.tool }}<span class="hint" v-if="s.args"> ({{ JSON.stringify(s.args) }})</span></div>
                <div class="trace-tool" v-else><span class="hint">✓ 综合诊断</span></div>
                <div class="trace-result" v-if="s.result">{{ s.result }}</div>
              </div>
            </div>
          </div>
        </div>
```

script——import 行加 `diagnoseAgent`；在 `const symptom = ...` 段落追加 agent 相关 ref 并改 `doDiagnose`：
```js
import { diagnose, similarCase, generateTicket, auditTicket, diagnoseAgent } from '../api'
```
```js
const agentMode = ref(false); const agentSteps = ref([]); const agentDegraded = ref(false); const traceOpen = ref(true)
async function doDiagnose() {
  if (!symptom.value.trim()) return
  loading.value = true; diag.value = null; agentSteps.value = []; agentDegraded.value = false
  try {
    if (agentMode.value) {
      const r = (await diagnoseAgent(symptom.value, modelType.value || null)).data
      diag.value = { diagnosis: r.diagnosis }      // 复用诊断卡片渲染（dimensions/sources 不显示）
      agentSteps.value = r.steps || []
      agentDegraded.value = !!r.degraded
    } else {
      diag.value = (await diagnose(symptom.value, modelType.value || null)).data
    }
  } catch (e) { show('诊断失败') } finally { loading.value = false }
}
```

style（`<style scoped>` 内）追加：
```css
.agent-toggle { user-select: none; }
.agent-trace { margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--border); }
.trace-step { display: flex; gap: 8px; margin-bottom: 8px; }
.trace-iter { flex-shrink: 0; width: 22px; height: 22px; border-radius: 50%; background: var(--primary); color: #fff; font-size: 12px; font-weight: 700; display: flex; align-items: center; justify-content: center; }
.trace-body { flex: 1; background: var(--surface-2); padding: 6px 10px; border-radius: var(--radius-sm); }
.trace-thought { font-size: 12px; color: var(--text); font-style: italic; margin-bottom: 2px; }
.trace-tool { font-size: 12px; color: var(--primary); font-weight: 600; }
.trace-result { font-size: 12px; color: var(--text-muted); margin-top: 2px; line-height: 1.5; white-space: pre-wrap; }
```

- [ ] **Step 3: 构建验证 + 手动**

```bash
cd frontend && npm run build   # 预期 ✓ built，无 error
```
手动（后端不带 --reload 起着）：admin 登录 → 故障诊断 → 勾「🔬 深度诊断(Agent)」→ 输入症状 → 点「深度诊断」→ 诊断卡片 + 可展开「Agent 思考过程」展示每步工具调用。

- [ ] **Step 4: Commit**
```bash
git add frontend/src/api/index.js frontend/src/views/Diagnose.vue
git commit -m "feat(agent): 前端深度诊断开关 + Agent 思考链 steps 渲染"
```

---

## Self-Review（已自检）

- **Spec 覆盖**：
  - function-calling agent loop → Task 1（provider）+ Task 3（循环）✅
  - 4 工具（检索/图谱/案例/两票）→ Task 2 ✅
  - 工具注册表 MCP 可复用 → Task 2 `TOOLS`/`_HANDLERS` 中立接口 ✅
  - `POST /domain/diagnose-agent`（6/min + get_current_user + write_log）→ Task 4 ✅
  - steps[] trace + 前端展示 → Task 3（结构）+ Task 5（渲染）✅
  - 超限/异常 → 回退 domain_service.diagnose → Task 3 `_fallback` ✅
  - 单工具失败不崩循环 → Task 2 `_run_tool` + Task 3 ✅
  - AGENT_ITERS + DOMAIN_CALLS(diagnose_agent) 指标 → Task 4 ✅
  - 测试（工具/循环/降级/provider/端点 smoke）→ Task 1/2/3/4 ✅
  - YAGNI（不做 MCP/问答 agent 化/流式/多 agent/记忆）→ 未引入 ✅

- **类型一致**：`chat_with_tools -> {content, tool_calls:[{id,name,arguments:dict}]}` 在 Task 1 定义、Task 3 消费一致；`steps[]` 字段（iter/thought/tool/args/result）Task 3 产出、Task 5 前端渲染字段（iter/thought/tool/args/result）一致；`diagnose_agent` 返回字段与 Task 5 消费（diagnosis/steps/degraded）一致 ✅

- **无占位符**：每步含实代码 / 实命令 / 实预期 ✅

- **热重载坑**：Global Constraints 明确"后端不带 --reload、改完手动重启、分支验证再合 main"——避免上次翻车 ✅
