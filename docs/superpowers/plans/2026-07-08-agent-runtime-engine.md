# S1 通用 Agent 引擎地基 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> ⚠️ **本机限制**：记忆 [[subagent-dispatch-broken-glm5]] 指出本机 Agent 工具派子 agent 会报"模型不存在"，故 **subagent-driven-development 不可用，必须用 superpowers:executing-plans inline 执行**。

**Goal:** 把诊断专用 `diagnose_agent_service` 抽象成 persona 驱动的通用 Agent 引擎（`agent_runtime`），并把 `diagnose_agent` 迁移为 persona="diagnose"，行为零回归。

**Architecture:** 新增 `agent_runtime.py`（Tool/ToolRegistry/Persona/run_agent/AgentResult）、`agent_tools.py`（4 工具迁入 + DEFAULT_REGISTRY）、`agent_personas.py`（DIAGNOSE_PERSONA）；`diagnose_agent_service.py` 瘦身为适配层（调 run_agent，返回 schema 不变）。ReAct 循环 / per-tool 异常隔离 / 降级 / Prometheus 指标全部沿用既有实现。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async / OpenAI 兼容 function-calling（deepseek/qwen/doubao 三家已实现 `chat_with_tools`）/ prometheus_client / pytest（**异步用 `asyncio.run`，项目无 pytest-asyncio**）。

## Global Constraints

- 异步测试用 `asyncio.run(coro)` 驱动，**不得**用 `pytest.mark.asyncio`（项目无 pytest-asyncio）。
- 端点层用 `py_compile` + import smoke 验证（项目无 TestClient 约定）。
- 新增 Prometheus 指标**必须**在 `init_metric_series()` 预注册 0 值（否则 `/metrics` 隐身，Grafana No data）。
- **不得改动** `/domain/diagnose-agent` 路由路径 / 请求响应 schema / 限流（6/min）。
- **不得改动** frontend（`Diagnose.vue` / `api/index.js`）——返回结构不变。
- **不得改动** `domain_service.py`（`_extract_json` 在 agent_runtime 自包含副本）。
- `agent_runtime.py` **不得** top-level import `agent_tools`（循环 import）；run_agent 内 lazy import `DEFAULT_REGISTRY`。
- commit message 风格：`feat(agent-runtime): ...` / `test(agent-runtime): ...` / `refactor(diagnose-agent): ...`。
- 测试命令统一在仓库根目录用项目 venv 执行：`pytest tests/test_agent_runtime.py -v`。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `backend/app/services/agent_runtime.py` | 引擎核心：Tool/ToolRegistry/Persona/AgentResult/run_agent/_fallback/_extract_json/_to_openai_tool_calls | 新增 |
| `backend/app/services/agent_tools.py` | 4 个 Tool（包装现有 service）+ _fmt_* 格式化器 + DEFAULT_REGISTRY | 新增 |
| `backend/app/services/agent_personas.py` | DIAGNOSE_PERSONA + _diagnose_fallback + _DIAGNOSE_SYSTEM | 新增 |
| `backend/app/core/metrics.py` | 新增 AGENT_CALLS / AGENT_TOOL_CALLS + init_metric_series 预注册 | 改动 |
| `backend/app/services/diagnose_agent_service.py` | 瘦身为适配层（调 run_agent，schema 不变） | 改动 |
| `tests/test_agent_runtime.py` | 全部单测 + 迁移不变性黄金回归 | 新增 |

---

## Task 1: agent_runtime 核心结构（Tool/ToolRegistry/Persona/AgentResult/辅助函数）

**Files:**
- Create: `backend/app/services/agent_runtime.py`
- Test: `tests/test_agent_runtime.py`

**Interfaces:**
- Produces: `Tool(name, description, parameters, handler)` + `.schema` 属性；`ToolRegistry`（`.register/.get/.schemas_for/.run`）；`Persona` dataclass；`AgentResult` dataclass；`_extract_json(text)->dict|None`；`_to_openai_tool_calls(tool_calls)->list`。

- [ ] **Step 1: 写失败测试（结构与辅助函数）**

创建 `tests/test_agent_runtime.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_agent_runtime.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.agent_runtime'`

- [ ] **Step 3: 写最小实现**

创建 `backend/app/services/agent_runtime.py`：

```python
"""通用 Agent 引擎：Tool/ToolRegistry/Persona/run_agent/AgentResult。

把诊断专用 agent 抽象成 persona 驱动的通用 ReAct 引擎。
循环 / per-tool 异常隔离 / 降级 / 指标沿用 diagnose_agent_service 既有实现。
工具集中定义在 agent_tools.py（run_agent 内 lazy import 避免循环）。
"""
import json
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.obs import degraded
from app.providers.factory import get_llm_provider

MAX_ITER_DEFAULT = 6


@dataclass
class Tool:
    """一个工具 = OpenAI schema + handler，绑成单一对象避免 schema/handler 不一致。"""
    name: str
    description: str
    parameters: dict
    handler: Callable[[AsyncSession, Optional[str], dict], Awaitable[str]]

    @property
    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}


class ToolRegistry:
    """工具注册表：register/get/schemas_for/run。run 内含 per-tool 异常隔离。"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def schemas_for(self, names: list[str]) -> list[dict]:
        out = []
        for n in names:
            t = self._tools.get(n)
            if t:
                out.append(t.schema)
        return out

    async def run(self, db, model_type, name: str, args: dict) -> tuple[str, bool]:
        """执行工具，返回 (result_text, error)。失败不抛，循环不崩。"""
        t = self._tools.get(name)
        if not t:
            return f"未知工具: {name}", True
        try:
            result = await t.handler(db, model_type, **(args or {}))
            return result, False
        except Exception as e:
            degraded(f"agent_tool_{name}", e)
            return f"工具 {name} 执行失败: {type(e).__name__}: {e}", True


@dataclass
class Persona:
    """场景配置：system prompt + 工具子集 + 参数 + 输出格式 + 降级目标。"""
    name: str
    system_prompt: str
    allowed_tools: list[str]
    max_iter: int = MAX_ITER_DEFAULT
    temperature: float = 0.2
    max_tokens: int = 1500
    output_format: str = "text"          # "json" | "text"
    fallback: Optional[Callable[[AsyncSession, str, Optional[str]], Awaitable[dict]]] = None
    config_source: str = "code"          # 预留 S5："code" | "db"


@dataclass
class AgentResult:
    answer: object                        # str（text）| dict（json）
    steps: list[dict]                     # [{iter, thought, tool, args, result, error}]
    iterations: int
    degraded: bool
    degrade_reason: Optional[str]
    latency_ms: int
    persona: str
    tools_used: list[str]


def _extract_json(ans: str) -> Optional[dict | list]:
    """从 LLM 输出中正则提取 JSON（自包含副本，与 domain_service 一致，不引入跨服务依赖）。"""
    m = re.search(r"(\{.*\}|\[.*\])", ans or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _to_openai_tool_calls(tool_calls):
    """内部 dict 形式 tool_calls → openai assistant 消息需要的结构。"""
    return [{"id": tc["id"], "type": "function",
             "function": {"name": tc["name"],
                          "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False)}}
            for tc in tool_calls]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_agent_runtime.py -v`
Expected: PASS（7 个测试全过）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_runtime.py tests/test_agent_runtime.py
git commit -m "feat(agent-runtime): Tool/ToolRegistry/Persona/AgentResult 核心结构与辅助函数"
```

---

## Task 2: agent_tools.py — 迁移 4 工具 + DEFAULT_REGISTRY

**Files:**
- Create: `backend/app/services/agent_tools.py`
- Test: `tests/test_agent_runtime.py`（追加）

**Interfaces:**
- Consumes: `agent_runtime.Tool`, `agent_runtime.ToolRegistry`（Task 1）
- Produces: `_t_search_regulation` / `_t_query_equipment_graph` / `_t_search_similar_case` / `_t_draft_ticket`；`_fmt_chunks` / `_fmt_cases` / `_fmt_ticket`；`DEFAULT_REGISTRY`（含 4 个 Tool）。

- [ ] **Step 1: 追加失败测试（工具包装下游 + 格式化器）**

在 `tests/test_agent_runtime.py` 末尾追加：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_agent_runtime.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.agent_tools'`

- [ ] **Step 3: 写实现**

创建 `backend/app/services/agent_tools.py`：

```python
"""Agent 工具集：把现有 service 包装成 Tool，返回 LLM 可读摘要。

从 diagnose_agent_service 迁移而来。后续 persona（S2/S3）只需在此新增 Tool 并注册。
"""
from app.services import domain_service, kg_service, retrieval_service
from app.services.agent_runtime import Tool, ToolRegistry

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


# ---------- schema ----------
_SCHEMA_QUERY = {"type": "object",
                 "properties": {"query": {"type": "string", "description": "检索关键词，如 '主变压器油温高 处置'"}},
                 "required": ["query"]}
_SCHEMA_ENTITY = {"type": "object",
                  "properties": {"entity": {"type": "string", "description": "设备名，如 '1号主变'"}},
                  "required": ["entity"]}
_SCHEMA_SYMPTOM = {"type": "object",
                   "properties": {"symptom": {"type": "string", "description": "故障症状描述"}},
                   "required": ["symptom"]}
_SCHEMA_TASK = {"type": "object",
                "properties": {"task": {"type": "string", "description": "操作任务，如 '1号主变由运行转检修'"}},
                "required": ["task"]}


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool("search_regulation",
                      "检索电网运维规程/手册/标准，获取故障处置的规程依据、限值、标准步骤。",
                      _SCHEMA_QUERY, _t_search_regulation))
    reg.register(Tool("query_equipment_graph",
                      "查知识图谱中设备的故障-处置因果链（设备→故障→处置 多跳）。",
                      _SCHEMA_ENTITY, _t_query_equipment_graph))
    reg.register(Tool("search_similar_case",
                      "查历史相似故障案例（故障案例库），看历史上类似故障怎么处理的。",
                      _SCHEMA_SYMPTOM, _t_search_similar_case))
    reg.register(Tool("draft_ticket",
                      "生成处置操作票草案（步骤/安措/风险）。诊断基本明确、需要处置步骤时调用。",
                      _SCHEMA_TASK, _t_draft_ticket))
    return reg


DEFAULT_REGISTRY = build_default_registry()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_agent_runtime.py -v`
Expected: PASS（12 个测试全过）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_tools.py tests/test_agent_runtime.py
git commit -m "feat(agent-runtime): 迁移4工具到 agent_tools + DEFAULT_REGISTRY"
```

---

## Task 3: metrics 新增 AGENT_CALLS / AGENT_TOOL_CALLS + 预注册

**Files:**
- Modify: `backend/app/core/metrics.py`（在 L72 `AGENT_ITERS` 之后新增 Counter；在 `init_metric_series` 内 `ROUTING_DECISION` 预注册之后追加）
- Test: `tests/test_agent_runtime.py`（追加）

**Interfaces:**
- Produces: `metrics.AGENT_CALLS`（Counter, labelnames=["persona"]）；`metrics.AGENT_TOOL_CALLS`（Counter, labelnames=["persona","tool"]）。

- [ ] **Step 1: 追加失败测试（指标预注册可见）**

在 `tests/test_agent_runtime.py` 末尾追加：

```python
from app.core import metrics
from prometheus_client import generate_latest


def test_agent_metrics_preregistered_in_registry():
    metrics.init_metric_series()
    text = generate_latest().decode("utf-8")
    # AGENT_CALLS diagnose 序列预注册
    assert "grid_agent_calls_total" in text
    assert 'persona="diagnose"' in text
    # AGENT_TOOL_CALLS 4 工具预注册
    assert "grid_agent_tool_calls_total" in text
    for t in ("search_regulation", "query_equipment_graph", "search_similar_case", "draft_ticket"):
        assert f'persona="diagnose" tool="{t}"' in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_agent_runtime.py::test_agent_metrics_preregistered_in_registry -v`
Expected: FAIL — `AssertionError`（`grid_agent_calls_total` 不在 /metrics 文本）

- [ ] **Step 3: 写实现**

在 `backend/app/core/metrics.py` 的 `AGENT_ITERS` 定义（L71-72）之后插入两个新 Counter：

```python
# 通用 Agent 引擎（S1）：persona 调用次数 + persona×工具 调用次数（为 S6 决策看板铺路）
AGENT_CALLS = Counter("grid_agent_calls_total", "Agent 引擎调用次数", ["persona"])
AGENT_TOOL_CALLS = Counter("grid_agent_tool_calls_total", "Agent 工具调用次数", ["persona", "tool"])
```

在 `init_metric_series()` 内、`ROUTING_DECISION` 预注册块（L194-196）之后、`except Exception:`（L197）之前追加：

```python
        # Agent 引擎（S1）：diagnose persona + 其 4 工具预注册 0 值
        AGENT_CALLS.labels("diagnose").inc(0)
        for _agent_tool in ("search_regulation", "query_equipment_graph",
                            "search_similar_case", "draft_ticket"):
            AGENT_TOOL_CALLS.labels("diagnose", _agent_tool).inc(0)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_agent_runtime.py::test_agent_metrics_preregistered_in_registry -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/metrics.py tests/test_agent_runtime.py
git commit -m "feat(metrics): AGENT_CALLS/AGENT_TOOL_CALLS 指标 + 预注册(diagnose persona)"
```

---

## Task 4: run_agent 引擎循环 + _fallback（核心）

**Files:**
- Modify: `backend/app/services/agent_runtime.py`（追加 run_agent / _fallback / _inc_metrics）
- Test: `tests/test_agent_runtime.py`（追加）

**Interfaces:**
- Consumes: Task 1 结构 + Task 2 `DEFAULT_REGISTRY`（lazy import）+ Task 3 `metrics.AGENT_CALLS/AGENT_TOOL_CALLS/AGENT_ITERS`
- Produces: `run_agent(db, persona, user_msg, model_type=None, registry=None) -> AgentResult`；`_fallback(db, persona, user_msg, model_type, steps, t0, reason) -> AgentResult`。

- [ ] **Step 1: 追加失败测试（mock provider + 自定义 registry，覆盖正常/降级/异常/per-tool）**

在 `tests/test_agent_runtime.py` 末尾追加：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_agent_runtime.py -k run_agent -v`
Expected: FAIL — `ImportError: cannot import name 'run_agent'`

- [ ] **Step 3: 写实现**

在 `backend/app/services/agent_runtime.py` 末尾追加：

```python
def _inc_metrics(persona: str, iterations: int) -> None:
    try:
        from app.core import metrics
        metrics.AGENT_CALLS.labels(persona).inc()
        metrics.AGENT_ITERS.observe(iterations)
    except Exception:
        pass


async def run_agent(db: AsyncSession, persona: Persona, user_msg: str,
                    model_type: Optional[str] = None,
                    registry: Optional[ToolRegistry] = None) -> AgentResult:
    """通用 ReAct 引擎：LLM 自主调工具多轮验证，persona 驱动全流程。"""
    if registry is None:
        from app.services.agent_tools import DEFAULT_REGISTRY
        registry = DEFAULT_REGISTRY
    t0 = time.perf_counter()
    provider = get_llm_provider(model_type)
    messages = [
        {"role": "system", "content": persona.system_prompt},
        {"role": "user", "content": user_msg},
    ]
    steps: list[dict] = []
    resp = None
    answer = None
    try:
        for i in range(1, persona.max_iter + 1):
            resp = await provider.chat_with_tools(
                messages, registry.schemas_for(persona.allowed_tools),
                temperature=persona.temperature, max_tokens=persona.max_tokens)
            if not resp.get("tool_calls"):
                steps.append({"iter": i, "thought": resp.get("content"), "tool": None,
                              "args": None, "result": None, "error": False})
                break
            messages.append({"role": "assistant", "content": resp.get("content") or "",
                             "tool_calls": _to_openai_tool_calls(resp["tool_calls"])})
            for tc in resp["tool_calls"]:
                result, err = await registry.run(db, model_type, tc["name"], tc.get("arguments"))
                steps.append({"iter": i, "thought": resp.get("content"), "tool": tc["name"],
                              "args": tc.get("arguments"), "result": (result or "")[:600], "error": err})
                try:
                    from app.core import metrics
                    metrics.AGENT_TOOL_CALLS.labels(persona.name, tc["name"]).inc()
                except Exception:
                    pass
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        else:
            # for-else：break 未触发 → 超 max_iter
            degraded("agent_max_iter", RuntimeError(f"persona={persona.name} max_iter={persona.max_iter}"))
            return await _fallback(db, persona, user_msg, model_type, steps, t0, "max_iter")

        if persona.output_format == "json":
            answer = _extract_json(resp.get("content") or "") or \
                {"summary": (resp.get("content") or "")[:500]}
        else:
            answer = resp.get("content") or ""
    except Exception as e:
        degraded("agent_error", e)
        return await _fallback(db, persona, user_msg, model_type, steps, t0, f"exception:{type(e).__name__}")

    iters = len(steps)
    _inc_metrics(persona.name, iters)
    return AgentResult(
        answer=answer, steps=steps, iterations=iters,
        degraded=False, degrade_reason=None,
        latency_ms=int((time.perf_counter() - t0) * 1000),
        persona=persona.name,
        tools_used=sorted({s["tool"] for s in steps if s["tool"]}),
    )


async def _fallback(db, persona: Persona, user_msg: str, model_type,
                    steps: list[dict], t0: float, reason: str) -> AgentResult:
    """降级：调 persona.fallback；失败再退到最小化结果。保留已收集 steps。"""
    answer = None
    if persona.fallback:
        try:
            answer = await persona.fallback(db, user_msg, model_type)
        except Exception as e:
            degraded("agent_fallback", e)
    _inc_metrics(persona.name, len(steps))
    return AgentResult(
        answer=answer or {"summary": "agent 降级，未能生成结果", "degradeReason": reason},
        steps=steps, iterations=len(steps),
        degraded=True, degrade_reason=reason,
        latency_ms=int((time.perf_counter() - t0) * 1000),
        persona=persona.name,
        tools_used=sorted({s["tool"] for s in steps if s["tool"]}),
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_agent_runtime.py -k run_agent -v`
Expected: PASS（5 个 run_agent 测试全过）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_runtime.py tests/test_agent_runtime.py
git commit -m "feat(agent-runtime): run_agent ReAct 循环 + _fallback(降级/per-tool隔离)"
```

---

## Task 5: agent_personas.py — DIAGNOSE_PERSONA + _diagnose_fallback

**Files:**
- Create: `backend/app/services/agent_personas.py`
- Test: `tests/test_agent_runtime.py`（追加）

**Interfaces:**
- Consumes: `agent_runtime.Persona`（Task 1）；`domain_service.diagnose`（既有）
- Produces: `DIAGNOSE_PERSONA`（Persona 实例）；`_diagnose_fallback(db, user_msg, model_type) -> dict`；`_DIAGNOSE_SYSTEM`（str）。

- [ ] **Step 1: 追加失败测试（persona 配置 + fallback 调 domain_service）**

在 `tests/test_agent_runtime.py` 末尾追加：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_agent_runtime.py -k diagnose_persona -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.agent_personas'`

- [ ] **Step 3: 写实现**

创建 `backend/app/services/agent_personas.py`：

```python
"""Agent persona 定义（S1 纯代码；DB+UI 留 S5）。

DIAGNOSE_PERSONA 由 diagnose_agent_service 迁移而来，system prompt / 工具集 / 输出格式
与原 _AGENT_SYSTEM / TOOLS / _extract_json 完全等价 → 诊断行为零回归。
"""
from app.services import domain_service
from app.services.agent_runtime import Persona

_DIAGNOSE_SYSTEM = """你是电网运维故障诊断专家。基于故障症状，通过调用工具自主收集证据（规程/图谱/历史案例）进行多轮交叉验证后给出诊断。
规则：
1) 每次可调用 0 个或多个工具；证据充分后停止调用工具，直接输出最终诊断。
2) 最终诊断必须输出严格 JSON：{"causes":[{"name":"可能原因","likelihood":"高/中/低","evidence":"资料依据","handling":"处置措施"}],"summary":"总体判断","risks":["风险点"]}
3) 原因按可能性从高到低排序；只基于工具收集的证据，证据不足如实说明；高风险处置（停电/接地/倒闸）须在 risks 标注。"""


async def _diagnose_fallback(db, user_msg, model_type):
    """降级：剥离 '故障症状：' 前缀后调 single-pass diagnose，返回 diagnosis dict。"""
    symptom = (user_msg or "").replace("故障症状：", "").strip()
    data = await domain_service.diagnose(db, symptom, model_type)
    return data.get("diagnosis", {"summary": "", "causes": []})


DIAGNOSE_PERSONA = Persona(
    name="diagnose",
    system_prompt=_DIAGNOSE_SYSTEM,
    allowed_tools=["search_regulation", "query_equipment_graph",
                   "search_similar_case", "draft_ticket"],
    max_iter=6,
    temperature=0.2,
    max_tokens=1500,
    output_format="json",
    fallback=_diagnose_fallback,
    config_source="code",
)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_agent_runtime.py -k diagnose -v`
Expected: PASS（2 个测试）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_personas.py tests/test_agent_runtime.py
git commit -m "feat(agent-runtime): DIAGNOSE_PERSONA + _diagnose_fallback(persona=diagnose)"
```

---

## Task 6: 迁移 diagnose_agent_service 为适配层 + 黄金回归 + 端点 smoke

**Files:**
- Modify: `backend/app/services/diagnose_agent_service.py`（全文替换为瘦适配层）
- Test: `tests/test_agent_runtime.py`（追加迁移不变性测试）

**Interfaces:**
- Consumes: Task 4 `run_agent` + Task 5 `DIAGNOSE_PERSONA`
- Produces: `diagnose_agent(db, symptom, model_type=None) -> dict`（签名与返回 schema 不变：`{symptom, diagnosis, steps, iterations, degraded, degradeReason, latencyMs}`）

- [ ] **Step 1: 追加迁移不变性黄金回归测试**

在 `tests/test_agent_runtime.py` 末尾追加：

```python
from app.services import diagnose_agent_service


def test_diagnose_agent_migration_returns_stable_schema(monkeypatch):
    """黄金回归：迁移后 diagnose_agent 返回 schema 与原实现一致（适配层映射正确）。"""
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
    # 返回 schema 不变（与原 diagnose_agent 一致）
    assert set(out.keys()) == {"symptom", "diagnosis", "steps", "iterations",
                               "degraded", "degradeReason", "latencyMs"}
    assert out["symptom"] == "1号主变油温高"
    assert out["degraded"] is False and out["degradeReason"] is None
    assert out["iterations"] == 3
    assert out["diagnosis"]["summary"] == "过热"
    assert out["diagnosis"]["causes"][0]["name"] == "风扇故障"
    assert [s["tool"] for s in out["steps"]] == ["search_regulation", "query_equipment_graph", None]


def test_endpoint_smoke_compile():
    """端点 smoke：路由模块可编译+导入（项目无 TestClient 约定）。"""
    import py_compile
    py_compile.compile("backend/app/routers/domain.py", doraise=True)
    from app.routers.domain import router
    paths = {r.path for r in router.routes}
    assert "/domain/diagnose-agent" in paths  # 路由路径不变
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_agent_runtime.py::test_diagnose_agent_migration_returns_stable_schema -v`
Expected: FAIL — 迁移前 `diagnose_agent_service.diagnose_agent` 内部仍调 `_run_tool`/`_HANDLERS`（fake 触发真实下游 → 报错），或返回 schema 不匹配。

- [ ] **Step 3: 写实现（全文替换 diagnose_agent_service.py）**

把 `backend/app/services/diagnose_agent_service.py` 全文替换为：

```python
"""Agentic 诊断（适配层）—— 通用 Agent 引擎的 persona=diagnose 入口。

引擎主体已迁至 agent_runtime / agent_tools / agent_personas。本文件仅保留
diagnose_agent(db, symptom, model_type) 适配层：调 run_agent(DIAGNOSE_PERSONA)，
并把 AgentResult 映射为既有返回 schema（路由/前端零改动）。
"""
from app.services.agent_personas import DIAGNOSE_PERSONA
from app.services.agent_runtime import run_agent


async def diagnose_agent(db, symptom, model_type=None):
    """Agentic 诊断：LLM 自主调工具多轮验证 → 既有响应 schema（不变）。"""
    result = await run_agent(
        db, DIAGNOSE_PERSONA, f"故障症状：{symptom}", model_type)
    return {
        "symptom": symptom,
        "diagnosis": result.answer,
        "steps": result.steps,
        "iterations": result.iterations,
        "degraded": result.degraded,
        "degradeReason": result.degrade_reason,
        "latencyMs": result.latency_ms,
    }
```

> 注：替换后原 `_HANDLERS` / `TOOLS` / `_t_*` / `_run_tool` / `_fmt_*` / `_AGENT_SYSTEM` / `MAX_ITER` / `_fallback` / `_to_openai_tool_calls` / `_inc_metric` 全部删除（已迁入 agent_runtime/agent_tools/agent_personas）。`routers/domain.py` 的 `from app.services import diagnose_agent_service` 与 `diagnose_agent_service.diagnose_agent(...)` 调用不变。

- [ ] **Step 4: 跑全量测试确认通过**

Run: `pytest tests/test_agent_runtime.py -v`
Expected: PASS（全部约 20 个测试，含迁移黄金回归 + 端点 smoke）

- [ ] **Step 5: 回归既有 diagnose_agent 测试（若存在）**

Run: `pytest tests/test_diagnose_agent.py -v`
Expected: PASS（迁移后行为等价）。若旧测试因引用已删除内部符号（`_HANDLERS`/`_run_tool` 等）而失败，按"测公共契约而非内部实现"原则更新断言，不改被测行为。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/diagnose_agent_service.py tests/test_agent_runtime.py
git commit -m "refactor(diagnose-agent): 迁移为 agent_runtime 适配层(路由/schema不变, 行为零回归)"
```

---

## Self-Review（写计划后自查）

**1. Spec 覆盖**：
- §4 架构抽象（Tool/ToolRegistry/Persona/run_agent/AgentResult）→ Task 1+4 ✓
- §5 数据流（ReAct 循环）→ Task 4 ✓
- §6 工具注册表 + persona 配置 → Task 2+5 ✓
- §6.3 persona 先代码、config_source 预留 → Task 5（config_source="code"）✓
- §7 错误处理与降级（per-tool 隔离 / max_iter / 异常 / json 解析）→ Task 1（隔离）+ Task 4（降级/json）✓
- §8 可观测（AGENT_CALLS/AGENT_TOOL_CALLS 预注册 + 沿用 AGENT_ITERS）→ Task 3+4 ✓
- §9 测试策略（黄金回归/runtime/persona/工具注册表/端点 smoke）→ 各 Task ✓
- §10 文件结构（4 新增 + 2 改动 + 不动项）→ 全覆盖 ✓
- §11 迁移兼容（路由/schema 不变）→ Task 6 ✓

**2. 占位扫描**：无 TBD/TODO/"适当处理"/"类似 Task N"。每个 step 含完整代码与精确命令。✓

**3. 类型一致性**：
- `ToolRegistry.run` 返回 `tuple[str, bool]` → Task 1 定义、Task 4 消费 `(result, err)` 一致 ✓
- `run_agent(..., registry=None) -> AgentResult` → Task 4 定义、Task 6 消费 `result.answer/steps/...` 一致 ✓
- `Persona.fallback` 签名 `(db, user_msg, model_type)` → Task 1 定义、Task 5 `_diagnose_fallback` 实现、Task 4 `_fallback` 调用一致 ✓
- `steps[].error` 字段 → Task 1 注释、Task 4 写入、Task 6 黄金回归不显式断言 error（向后兼容）✓
- 指标名 `AGENT_CALLS`/`AGENT_TOOL_CALLS`/`AGENT_ITERS` → Task 3 定义、Task 4 `_inc_metrics` 与 run_agent 内引用一致 ✓

---

## Execution Handoff

计划已保存到 `docs/superpowers/plans/2026-07-08-agent-runtime-engine.md`。

两种执行方式：

1. **Inline 执行（本机唯一可用）** — 用 `superpowers:executing-plans` 在本会话按 task 顺序执行，批量推进 + checkpoint 复核。**本机 subagent-driven 因 [[subagent-dispatch-broken-glm5]] 不可用，故推荐此方式。**
2. ~~Subagent-Driven~~ — 本机不可用（Agent 工具派子 agent 报"模型不存在"）。

**下一步**：确认后我用 `superpowers:executing-plans` 从 Task 1 开始 inline 执行。
