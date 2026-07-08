# S1：通用 Agent 引擎地基（agent_runtime 抽象 + persona 驱动）

- **日期**：2026-07-08
- **状态**：设计已确认，待写实现计划（writing-plans）
- **子项目**：S1（方案 C 拆解的第 1 个，地基）
- **关联现状**：泛化自 [`2026-07-02-agentic-diagnose-design.md`](./2026-07-02-agentic-diagnose-design.md)（诊断专用 agent，已落地）。本 spec 把其中"刻意设计成中立接口、Spec 2/MCP 可直接包装"的 `TOOLS` 注册表正式兑现为通用 `ToolRegistry`。
- **关联后续**：S2 问答 Agent、S3 告警处置、S4 工具审计/权限、S5 persona 管理 UI、S6 决策看板均依赖本子项目。
- **调研工具**：codegraph（代码事实核实）+ superpowers/brainstorming（设计流程）

---

## 1. 背景与目标

系统已有一个**诊断专用 Agentic 服务** `backend/app/services/diagnose_agent_service.py`（设计见 `2026-07-02-agentic-diagnose-design.md`）：LLM 用 OpenAI function-calling 自主多轮调用工具（检索/图谱/案例/两票）做交叉验证诊断，返回诊断结果 + 思考链 `steps[]`。其 ReAct 循环、工具注册表、per-tool 异常隔离、降级、指标已相当完整，但 system prompt、工具集、输出格式、降级目标全部**硬编码在诊断域**，无法被其他场景（通用问答、告警处置等）复用。原 spec 早已在 `TOOLS` 注册表处埋下"接口中立、Spec 2/MCP 可直接包装"的泛化意图。

**本子项目目标**：把诊断专用 agent 抽象成通用 Agent 引擎，引入"persona（场景配置）"维度，让引擎本身零硬编码业务，所有差异由 persona 驱动；同时把现有 `diagnose_agent` 迁移为引擎的一个 persona，**行为不变**，作为地基正确性的验收锚点。

**一句话目标**：抽 `agent_runtime`（Tool/ToolRegistry/Persona/run_agent），`diagnose_agent` 迁移成 persona="diagnose" 后诊断行为零回归，为 S2~S6 提供"只加配置不改引擎"的扩展底座。

## 2. 范围

### 2.1 本子项目做（S1）

- 抽象 `Tool` / `ToolRegistry` / `Persona` / `run_agent` / `AgentResult`。
- 把现有 4 个工具 handler + schema 迁成统一 `Tool` 对象，集中到 `agent_tools.py`。
- 用 Python dataclass 定义 persona（含 diagnose）。
- 迁移 `diagnose_agent_service.diagnose_agent` 为"定义 diagnose persona + 调 run_agent"。
- 新增 Prometheus 指标 `AGENT_CALLS` / `AGENT_TOOL_CALLS`（沿用 `AGENT_ITERS`）。
- TDD 测试：runtime 单测 + persona 配置测试 + 工具注册表测试 + **迁移不变性黄金回归**。

### 2.2 本子项目不做（留给后续子项目）

- 通用问答 persona 接入主问答 → **S2**
- 告警接入协议 + 告警 persona + 推送 → **S3**
- 工具调用审计持久化 + 权限 → **S4**
- persona 的 DB 持久化 + admin 管理 UI → **S5**（S1 仅在 `Persona` 接口预留 `config_source` 字段）
- agent 决策 Grafana 看板 → **S6**（S1 先把指标埋点铺好）
- MCP server 对外暴露（原 spec 称 "Spec 2"）→ 后续，S1 的 `ToolRegistry` 为其铺路

### 2.3 不变性约束

- `/domain/diagnose-agent` 路由路径、请求/响应 schema、限流（6/min）**完全不变**。
- 前端 `Diagnose.vue` **零改动**（返回结构不变）。
- 迁移后诊断结果与思考链结构等价于迁移前（黄金回归保证）。

## 3. 现状分析（codegraph 核实）

`diagnose_agent_service.py` 现有结构（行号对应当前源码；设计详见 `2026-07-02-agentic-diagnose-design.md`）：

- **工具 handler**（L17-38）：`_t_search_regulation` / `_t_query_equipment_graph` / `_t_search_similar_case` / `_t_draft_ticket`，每个包装现有 service 并返回 LLM 可读摘要。
- **handler 注册表**（L41-46）：`_HANDLERS = {name: handler}`。
- **工具分发**（L49-58）：`_run_tool` —— `try/except` 包裹，工具失败返回错误串不抛（**per-tool 异常隔离已实现**），并 `degraded(...)` 上报。
- **OpenAI schema**（L90-115）：`TOOLS` —— 与 `_HANDLERS` 键对应的 function-calling schema（**分离的两个 dict，靠键对应，存在不一致隐患**）。
- **system prompt**（L117-121）：`_AGENT_SYSTEM` —— 诊断专用，要求输出严格 JSON。
- **ReAct 循环**（L141-181）：`diagnose_agent` —— `MAX_ITER=6`，`chat_with_tools` → 有 tool_calls 则执行并记 messages/steps → 无则 break → `_extract_json` 解析。
- **降级**（L184-194）：`_fallback` —— 调 `domain_service.diagnose` single-pass，保留已收集 steps。
- **指标**（L132-138）：`DOMAIN_CALLS.labels("diagnose_agent")` + `AGENT_ITERS.observe(iterations)`。
- **格式化器**（L62-83）：`_fmt_chunks` / `_fmt_cases` / `_fmt_ticket`。
- **Provider**：`LLMProvider.chat_with_tools` 已在 deepseek/qwen/doubao 三家实现（均 OpenAI 兼容，均支持 function-calling），返回 `{"content", "tool_calls":[{"id","name","arguments"}]}`。

文件头注释（L1-5）已明写：*"工具定义成注册表（TOOLS + _HANDLERS），接口中立——Spec 2(MCP) 直接复用"* —— 本设计是将其兑现。

**复用率评估**：循环逻辑、异常隔离、降级、指标 90% 原样复用；主要工作是把"分离的 `_HANDLERS`+`TOOLS`"合并为 `Tool` 对象、把硬编码 prompt/参数提取为 `Persona`。

## 4. 架构与核心抽象

新增 `backend/app/services/agent_runtime.py`，定义三件套 + 入口 + 结果。

### 4.1 Tool（合并 schema 与 handler）

```python
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict            # JSON Schema（即原 ToolDef.json_schema / TOOLS[].function.parameters）
    handler: Callable[[AsyncSession, str | None, dict], Awaitable[str]]
    # handler 签名: async (db, model_type, **args) -> str（LLM 可读摘要）

    @property
    def schema(self) -> dict:   # OpenAI function-calling schema
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}
```

> **质量改进**：现有 `_HANDLERS` 与 `TOOLS` 分离、靠键对应，加 schema 忘加 handler 会运行时才暴露。`Tool` 把二者绑成单一对象，注册时即保证一致。
> **MCP 衔接**：`Tool` 即原 spec `ToolDef` 的正式落地，字段中立，未来 MCP（Spec 2）可直接把 `ToolRegistry` 包装成 MCP server，零返工。

### 4.2 ToolRegistry

```python
class ToolRegistry:
    def register(self, tool: Tool) -> None
    def get(self, name: str) -> Tool | None
    def schemas_for(self, names: list[str]) -> list[dict]   # 给 chat_with_tools 用
    async def run(self, db, model_type, name: str, args: dict) -> tuple[str, bool]
        # 返回 (result_text, error)；内含 try/except：
        #   未知工具→("未知工具: {name}", True)
        #   handler 异常→("工具 {name} 执行失败: ...", True) 并 degraded()
```

`run` 即现有 `_run_tool` 的泛化版，**per-tool 异常隔离原样保留**，并额外回传 `error` 标志（对齐原 spec L101 "step 标 error:true" 的设计意图——现实现未落地，S1 顺带补齐）。

### 4.3 Persona（场景配置）

```python
@dataclass
class Persona:
    name: str                       # "diagnose" / "qa" / "alert" ...
    system_prompt: str
    allowed_tools: list[str]        # 工具子集（按名取）
    max_iter: int = 6
    temperature: float = 0.2
    max_tokens: int = 1500
    output_format: str = "text"     # "json" | "text"，决定是否 _extract_json
    fallback: Callable[[AsyncSession, str, str | None], Awaitable[dict]] | None = None
    config_source: str = "code"     # 预留 S5："code" | "db"，S1 仅用 "code"
```

### 4.4 run_agent（统一入口）

```python
async def run_agent(
    db: AsyncSession, persona: Persona, user_msg: str,
    model_type: str | None = None, registry: ToolRegistry = DEFAULT_REGISTRY,
) -> AgentResult
```

引擎本身零硬编码业务：system prompt、工具子集、迭代上限、温度、输出解析、降级目标全部来自 `persona`。

### 4.5 AgentResult

```python
@dataclass
class AgentResult:
    answer: str | dict          # output_format="text"→str；"json"→dict
    steps: list[dict]           # [{iter, thought, tool, args, result, error}]
    iterations: int
    degraded: bool
    degrade_reason: str | None
    latency_ms: int
    persona: str
    tools_used: list[str]       # 去重后的实际调用工具名
```

`steps[].error` 为 bool（该工具是否出错），对齐原 spec 设计意图；`diagnose_agent` 适配层映射到现有响应时该字段可保留或省略（前端不依赖，向后兼容）。

## 5. 数据流（ReAct 循环）

```
入: db, persona, user_msg, model_type, registry
t0 = perf_counter()
messages = [{system: persona.system_prompt}, {user: user_msg}]
steps = []
try:
    resp = None
    for i in 1..persona.max_iter:
        resp = provider.chat_with_tools(
            messages,
            registry.schemas_for(persona.allowed_tools),
            temperature=persona.temperature,
            max_tokens=persona.max_tokens)
        if not resp.tool_calls:
            steps.append({iter:i, thought:resp.content, tool:None, args:None, result:None, error:False})
            break
        messages.append({assistant, content:resp.content, tool_calls: normalized})
        for tc in resp.tool_calls:
            result, err = await registry.run(db, model_type, tc.name, tc.args)
            steps.append({iter:i, thought:resp.content, tool:tc.name, args:tc.args,
                          result:result[:600], error:err})
            messages.append({tool, tool_call_id:tc.id, content:result})
    else:
        # for-else：break 未触发 → 超 max_iter
        degraded("agent_max_iter", RuntimeError(f"persona={persona.name} max_iter={persona.max_iter}"))
        return _fallback(...)

    if persona.output_format == "json":
        answer = _extract_json(resp.content) or {"summary": (resp.content or "")[:500]}
    else:
        answer = resp.content or ""
except Exception as e:
    degraded("agent_error", e)
    return _fallback(...)

return AgentResult(answer, steps, len(steps), ..., persona=persona.name,
                   tools_used=sorted(set(s.tool for s in steps if s.tool)),
                   latency_ms=int((perf_counter()-t0)*1000))
```

`_fallback`：若 `persona.fallback` 不为 None 则调用并包装为 `AgentResult(degraded=True, degrade_reason=...)`，保留已收集 `steps`；否则返回最小化降级结果（answer=错误说明）。

`normalized` = `_to_openai_tool_calls`（现有函数原样复用）。

**persona 在每步生效**：角色 prompt（system）、工具子集（schemas_for）、迭代上限（max_iter）、采样温度（temperature）、输出解析（output_format）、降级目标（fallback）。

## 6. 工具注册表与 persona 配置

### 6.1 工具集中注册（`agent_tools.py`）

把现有 4 个 `_t_*` handler + schema 迁成 `Tool` 对象，构建 `DEFAULT_REGISTRY`：

| 工具名 | handler（迁移自） | 包装的现有 service |
|---|---|---|
| `search_regulation` | `_t_search_regulation` | `retrieval_service.mixed_search` |
| `query_equipment_graph` | `_t_query_equipment_graph` | `kg_service.graph_context` |
| `search_similar_case` | `_t_search_similar_case` | `domain_service.similar_case` |
| `draft_ticket` | `_t_draft_ticket` | `domain_service.generate_ticket` |

格式化器 `_fmt_chunks` / `_fmt_cases` / `_fmt_ticket` 一并迁入 `agent_tools.py`。`_TOPK=5` 保留为模块常量。

> 后续扩展点：S2 可加 `query_history`（对话历史）、S3 可加 `query_alert_history`（告警历史）等，只需在 `agent_tools.py` 新增 `Tool` 并注册，不改引擎。

### 6.2 persona 定义（`agent_personas.py`，S1 纯代码）

```python
DIAGNOSE_PERSONA = Persona(
    name="diagnose",
    system_prompt=_DIAGNOSE_SYSTEM,            # 现有 _AGENT_SYSTEM 原文迁入
    allowed_tools=["search_regulation", "query_equipment_graph",
                   "search_similar_case", "draft_ticket"],
    max_iter=6, temperature=0.2, max_tokens=1500,
    output_format="json",
    fallback=_diagnose_fallback,               # 包装 domain_service.diagnose
    config_source="code",
)
```

`_diagnose_fallback(db, symptom, model_type)`：调 `domain_service.diagnose`，返回 `{"diagnosis": data.get("diagnosis", ...)}`，与现有 `_fallback` 行为等价。

### 6.3 关键取舍：persona 先代码，DB 留给 S5

persona 配置**先以 Python dataclass 定义**，不上 DB、不加迁移、不做 admin UI（YAGNI）。理由：

- 复用本项目一贯风格（开关/阈值在 `.env` 与 settings，配置随代码版本）。
- S1 的目标是稳固引擎抽象，persona 管理是独立产品能力（S5）。
- `Persona.config_source` 字段预留，S5 实现 DB 加载时只需扩展加载路径，不改 `Persona` 结构与 `run_agent` 逻辑。

## 7. 错误处理与降级

| 场景 | 处理 | 来源 |
|---|---|---|
| 单个工具 handler 异常 | `ToolRegistry.run` try/except → 返回错误串 + `error=True`，循环继续 | 沿用 `_run_tool` |
| 未知工具名 | `ToolRegistry.run` → 返回 `"未知工具: {name}"` + `error=True` | 沿用 |
| 超 `max_iter` | for-else → `persona.fallback`，`degraded=True` | 沿用 `_fallback` |
| 引擎整体异常 | 外层 try/except → `persona.fallback` | 沿用 |
| `output_format="json"` 但 LLM 未返回合法 JSON | `_extract_json` 容错（正则提取），失败回退 `{"summary": 原文[:500]}` | 沿用 |
| 工具结果过长 | `result[:600]` 截断回填 | 沿用 |

> 注：现有 deepseek/qwen/doubao 三家 provider 均已实现 `chat_with_tools` 且支持 function-calling，故 S1 不存在"provider 不支持工具调用"的风险。约束改为：**未来新增 provider 必须实现 `chat_with_tools`**，否则以该 persona 的 `fallback` 兜底。

## 8. 可观测（Prometheus 指标）

在 `backend/app/core/metrics.py` 预注册（沿用本项目"指标未触碰前在 /metrics 隐身 → 预注册 0 值"的约定）：

- `AGENT_CALLS = Counter(labelnames=["persona"])` —— 每个 persona 调用次数。
- `AGENT_TOOL_CALLS = Counter(labelnames=["persona", "tool"])` —— persona×工具 调用次数。
- `AGENT_ITERS = Histogram(...)` —— **已存在，沿用**，buckets=(1,2,3,4,5,6,inf)。
- `AGENT_LATENCY = Histogram(labelnames=["persona"])` —— 端到端延迟（可选，参考现有 `LLM_LATENCY`）。

`run_agent` 在返回前埋点：`AGENT_CALLS.labels(persona.name).inc()`，每个工具执行后 `AGENT_TOOL_CALLS.labels(persona, tool).inc()`。这些埋点为 S6 决策看板提供数据源。

## 9. 测试策略（TDD）

测试文件：`tests/test_agent_runtime.py`。**项目无 pytest-asyncio，异步测试用 `asyncio.run` 驱动**（与现有 `tests/test_diagnose_agent.py` 一致）；端点层用 `py_compile` + import smoke（项目无 TestClient 约定）。

### 9.1 迁移不变性黄金回归（地基验收锚点）

用 mock provider 注入**确定性 tool_calls 序列**（例如：第 1 轮调 `search_regulation`+`query_equipment_graph`，第 2 轮调 `search_similar_case`，第 3 轮无 tool_calls 返回固定 JSON），跑 `DIAGNOSE_PERSONA`，锁定 `AgentResult` 的 `steps` 结构与 `answer` 解析结果为 golden。任何后续重构若破坏该输出即测试失败。这是"地基正确"的核心证据。

### 9.2 runtime 单测（mock provider，asyncio.run）

- 循环轮数正确（N 轮 tool_calls 后 break → iterations=N）。
- 工具分发正确（tool_calls 全部执行并记入 messages 与 steps）。
- 超 `max_iter` → `degraded=True`、`degrade_reason` 含 max_iter、走 fallback。
- 引擎异常（provider 抛错）→ `degraded=True`、走 fallback。
- per-tool 失败隔离（某 handler 抛错 → steps 记错误串且 `error=True`、其余工具与循环不受影响）。

### 9.3 persona 配置测试

- 不同 persona 的 `schemas_for(allowed_tools)` 返回不同工具子集。
- `system_prompt` 正确注入 messages[0]。
- `output_format="json"` 走 `_extract_json`，`"text"` 走原文。
- `fallback` 各 persona 指向正确目标。

### 9.4 工具注册表测试

- 未知工具 → `run` 返回 `"未知工具: ..."` 且 `error=True`。
- handler 异常 → `run` 返回错误串且不抛、`degraded` 被调用、`error=True`。
- `Tool.schema` 输出符合 OpenAI function-calling 结构。
- schema 与 handler 一致性（`Tool` 合并后天然保证）。

### 9.5 端点 smoke

`/domain/diagnose-agent` 迁移后：`py_compile` + import smoke 通过（路由/schema 不变）。

### 9.6 集成（可选，依赖真实 provider）

真实 provider 跑一次 diagnose persona，断言不 regression（结构与单测一致，内容允许随模型变化）。

## 10. 文件结构

### 新增

- `backend/app/services/agent_runtime.py` —— `Tool` / `ToolRegistry` / `Persona` / `run_agent` / `AgentResult` / `_fallback` / `_extract_json`（自包含副本，与 `domain_service` 现有实现一致；S1 不改 `domain_service`，避免跨服务依赖与循环 import）。
- `backend/app/services/agent_tools.py` —— 4 个 `Tool` 定义 + `_fmt_*` 格式化器 + `DEFAULT_REGISTRY`。
- `backend/app/services/agent_personas.py` —— `DIAGNOSE_PERSONA` + `_diagnose_fallback` + `_DIAGNOSE_SYSTEM`。
- `tests/test_agent_runtime.py` —— 上述全部测试。

### 改动

- `backend/app/services/diagnose_agent_service.py` —— **瘦身**：删除 `_HANDLERS`/`TOOLS`/`_t_*`/`_run_tool`/`_fmt_*`/`_AGENT_SYSTEM`/循环逻辑（迁出），保留 `diagnose_agent(db, symptom, model_type)` 作为适配层：调 `agent_runtime.run_agent(db, DIAGNOSE_PERSONA, f"故障症状：{symptom}", model_type)`，并把 `AgentResult` 适配为现有返回 schema（`{symptom, diagnosis, steps, iterations, degraded, degradeReason, latencyMs}`），**键名与结构完全不变**。
- `backend/app/core/metrics.py` —— 新增 `AGENT_CALLS` / `AGENT_TOOL_CALLS`（/ `AGENT_LATENCY`）预注册 0 值。

### 不动

- `backend/app/routers/domain.py` 的 `/domain/diagnose-agent` 路由（路径/限流/schema 不变）。
- `frontend/src/views/Diagnose.vue` 与 `frontend/src/api/index.js` 的 `diagnoseAgent`（返回结构不变 → 前端零改动）。

## 11. 迁移与兼容（验收锚点）

1. `/domain/diagnose-agent` 路由路径、请求体（`DiagnoseAgentRequest{symptom, modelType}`）、响应体（`{symptom, diagnosis, steps, iterations, degraded, degradeReason, latencyMs}`）、限流（6/min）**全部不变**。
2. `diagnose_agent_service.diagnose_agent` 函数签名 `(db, symptom, model_type=None) -> dict` **不变**。
3. 黄金回归测试（9.1）通过 = 地基抽象未改变诊断行为。
4. 现有 `Diagnose.vue` 前端无需任何改动即可正常工作。

## 12. 依赖与后续子项目

S1 完成后，S2~S6 在此地基上扩展，**只加配置/入口/工具，不改引擎核心**：

| 后续 | 如何基于 S1 扩展 |
|---|---|
| **S2 问答 Agent** | 新增 `QA_PERSONA`（`output_format="text"`, `fallback=qa_service.answer`）+ 主问答加 agentMode 开关调 `run_agent` |
| **S3 告警处置** | 新增 `ALERT_PERSONA` + 告警 ingestion 端点触发 `run_agent` + 复用 WS 推送 |
| **S4 工具审计/权限** | 在 `ToolRegistry.run` 加调用日志持久化 + 按 tenant/role 过滤 `allowed_tools` |
| **S5 persona 管理 UI** | 实现 `config_source="db"` 加载路径 + admin CRUD 界面 |
| **S6 决策看板** | 基于 S1 埋点的 `AGENT_CALLS`/`AGENT_TOOL_CALLS`/`AGENT_ITERS` 建 Grafana 面板 |
| **MCP server（Spec 2）** | 直接包装 `ToolRegistry` 为 MCP server，新增外部 MCP 工具注册进同一注册表 |

## 13. 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| 迁移引入行为差异 | 诊断回归 | 黄金回归测试（9.1）+ 前端零改动约束 + 路由/schema 不变三重把关 |
| LLM 反复调用同类工具导致无效循环 | 浪费 token、延迟升高 | `max_iter` 兜底；S1 暂不做"连续相同调用检测"，列入 S2 优化项 |
| `output_format="json"` 时 LLM 不返回合法 JSON | 解析失败 | 沿用 `_extract_json` 正则容错 + 失败回退 summary |
| 工具摘要过长撑爆上下文 | 后续轮 token 超限 | `result[:600]` 截断（现有做法，保留） |
| 未来新增 provider 未实现 `chat_with_tools` | 该 provider 下 agent 不可用 | 接口约束 + persona `fallback` 兜底 |

---

## 附录：决策记录

- **D1**：persona 先代码定义、DB 留 S5 —— YAGNI，复用项目配置风格。（用户已确认）
- **D2**：`Tool` 合并 schema+handler —— 消除现有分离 dict 的不一致隐患；即原 spec `ToolDef` 的正式落地，为 MCP（Spec 2）铺路。
- **D3**：保留 `/domain/diagnose-agent` 路由与 schema 不变 —— 用迁移不变性作为地基验收锚点，降低回归风险。
- **D4**：S1 不做新 persona（qa/alert）的接入 —— 严格限制范围，每个子项目独立 spec。
- **D5**：对齐现有约定 —— 测试用 `asyncio.run`（无 pytest-asyncio）、端点 py_compile+import smoke、指标预注册 0 值、三家 provider 均支持 function-calling（修正初版"provider 可能不支持"的误判，该误判源于只读了 deepseek_llm 未核实 qwen/doubao）。
