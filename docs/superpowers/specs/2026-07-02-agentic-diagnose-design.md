# Agentic 诊断 — 设计 spec

> 日期：2026-07-02 ｜ 选型：OpenAI function-calling agent loop（用户确认）
> 状态：待用户审阅 ｜ 后续：Spec 2 = MCP（把工具对外暴露 + 接外部实时数据）

## 背景与目标

现有 `/domain/diagnose` 是**固定管线**：多查询分解 → 并行检索 → 图谱因果 → LLM 综合。流程写死，LLM 只在最后写答案。复杂故障需要老工程师式的**多轮交叉验证**（查规程→查因果→查案例→再补一查），固定管线做不到。

**目标**：新增 `/domain/diagnose-agent`，把"调几次、调哪个工具、何时停"交给 LLM 自主决策（Agentic RAG）。现有快速 diagnose 保留不动，两者并存可对比。诊断过程以 `steps[]` 透出，可解释、可追溯（电网强监管要求）。

## 架构：function-calling agent 循环

```
症状 ──▶ diagnose_agent_service.diagnose_agent()
          │  messages = [system(角色+工具指引), user(症状)]；tools = TOOLS 注册表
          │  for i in 1..MAX_ITER(6):
          │      resp = provider.chat_with_tools(messages, tools_schema)
          │      if not resp.tool_calls:                          # LLM 给最终答案
          │          steps.append({iter:i, thought:resp.content, tool:null, result:null})  # 收尾思考
          │          break
          │      for tc in resp.tool_calls:
          │          result = await _run_tool(tc.name, tc.args)   # 异常不崩循环
          │          steps.append({iter:i, thought:resp.content, tool:tc.name, args:tc.args, result, error?})
          │          messages.append(tool 结果消息)
          │  else: → 超限降级（for-else：break 未触发）
          │  diagnosis = _extract_json(resp.content) or 降级
          └─ return {diagnosis, steps, iterations, degraded, degradeReason, latencyMs}
```

**为什么 function-calling**：原生结构化（不靠正则）、三家 provider（DeepSeek/Qwen/豆包，均 OpenAI 兼容）都支持、零新依赖、自写循环 ~150 行。

## 组件

### 后端

- **`backend/app/services/diagnose_agent_service.py`（新）**
  - `TOOLS: list[ToolDef]` —— 工具注册表（`ToolDef = {name, description, json_schema, async handler}`）。**这层接口刻意设计成 MCP 可直接包装**（Spec 2 复用）。
  - `diagnose_agent(db, symptom, model_type) -> dict` —— 循环主体。
  - `_run_tool(db, name, args) -> str` —— 分发执行，返回 LLM 可读摘要（截断超长结果）。
  - `_fallback(db, symptom, model_type, reason, steps) -> dict` —— 降级到现有 `domain_service.diagnose()`。

- **4 个工具**（包装现有 service，不重复造轮子）：

  | 工具名 | handler | 作用 |
  |---|---|---|
  | `search_regulation` | `retrieval_service.mixed_search` | 查运维规程/手册 |
  | `query_equipment_graph` | `kg_service.graph_context` | 查设备-故障-处置因果链 |
  | `search_similar_case` | `domain_service.similar_case` | 查历史相似故障案例 |
  | `draft_ticket` | `domain_service.generate_ticket` | 生成处置操作票草案 |

- **Provider 扩展**（`providers/base.py` + `llm/{deepseek,qwen,doubao}_llm.py`）
  - `LLMProvider` 加 `async chat_with_tools(messages, tools, tool_choice="auto", temperature=0.2, max_tokens=2048) -> dict`
  - 返回 `{"content": str|None, "tool_calls": [{"id","name","arguments":dict}] | None}`
  - 三家实现：openai SDK 多传 `tools=` / `tool_choice=`，多取 `message.tool_calls`（每家 ~10 行）。

- **路由**（`routers/domain.py`）：`POST /domain/diagnose-agent`，`@limiter.limit("6/minute")`（agent 耗资源，限流更紧）+ `Depends(get_current_user)`（普通用户可用）+ `write_log("深度诊断", ...)`。

- **Schema**（`schemas/domain.py`）：`DiagnoseAgentRequest(symptom: str, modelType: Optional[str] = None)`。

- **指标**（`core/metrics.py`）：复用 `DOMAIN_CALLS.labels("diagnose_agent")` + `DEGRADED.labels("diagnose_agent_*")`；新增 `AGENT_ITERS = Histogram("grid_agent_iters", "诊断 agent 循环深度", buckets=(1,2,3,4,5,6,float("inf")))`；`init_metric_series` 预注册 DOMAIN_CALLS label。

### 前端

- **`frontend/src/views/Diagnose.vue` 故障诊断 tab**：输入区加「🔬 深度诊断(Agent)」开关；结果区在现有诊断卡片下方加**可折叠「Agent 思考过程」**，按 step 渲染（轮次徽章 / thought / `工具名(args)` / 结果摘要 / error 标红）。诊断卡片本身复用现有 cause 渲染。
- **`frontend/src/api/index.js`**：加 `diagnoseAgent(symptom, modelType) => request.post('/domain/diagnose-agent', ...)`。

## 数据结构

```python
# 请求：DiagnoseAgentRequest
{"symptom": "1号主变上层油温 95℃ 持续上升，负载 70%", "modelType": "deepseek"}

# 响应
{
  "symptom": "...",
  "diagnosis": {                                          # 与现有 diagnose 同结构 → 前端复用
    "causes": [{"name":"...", "likelihood":"高/中/低", "evidence":"...", "handling":"..."}],
    "summary": "...", "risks": ["..."]
  },
  "steps": [
    {"iter":1, "thought":"先查规程", "tool":"search_regulation",
     "args":{"query":"主变油温高"}, "result":"规程：检查冷却系统/负荷/温度限值…"},
    {"iter":2, "thought":"查因果链", "tool":"query_equipment_graph",
     "args":{"entity":"1号主变"}, "result":"风扇故障→过热"},
    {"iter":3, "thought":"证据充分，综合诊断", "tool":null, "result":null}
  ],
  "iterations": 3,
  "degraded": false,
  "degradeReason": null,        # "max_iter" | "exception:..." | "json_parse"
  "latencyMs": 12345
}
```

最终一轮 prompt 要求 LLM 输出**与现有 diagnose 完全一致**的 `{causes,summary,risks}` JSON，前端诊断卡片零改动。

## 错误处理 / 安全网

- **`MAX_ITER=6`** 到顶 → `degraded("diagnose_agent_maxiter")` → `_fallback` 调现有 `diagnose()`，返回其 diagnosis + 已收集 steps + `degraded:true, degradeReason:"max_iter"`。
- **循环中任何异常** → `degraded("diagnose_agent_error", e)` → 同样 `_fallback`。
- **单个工具失败** → 不崩循环；错误摘要作为该 tool 的 result 回填给 LLM（LLM 可换工具/换词重试），step 标 `error:true`。
- **LLM 最终输出非合法 JSON** → `_extract_json` 兜底（镜像 `domain_service` 既有做法）；仍失败 → 降级。
- **限流 6/min**：agent 多轮 LLM 调用耗资源，比普通接口紧。
- **成本**：多轮 LLM 调用；诊断低频场景可接受。精确计费留给后续 token-metering spec。

## 测试（`tests/test_diagnose_agent.py`）

- **工具注册表**：每个 handler 正确包装下游 service、返回 LLM 可读摘要（mock 下游）。
- **`chat_with_tools`**：mock openai client，先返回 tool_calls、再返回 final → 验证返回结构。
- **agent 循环**：mock provider 脚本化（第1轮→检索，第2轮→图谱，第3轮→final）→ 断言 steps 顺序/长度、iterations、final diagnosis 结构。用 `asyncio.run`（项目无 pytest-asyncio）。
- **降级**：① max_iter 触发 → `degraded:true`、走 fallback；② 循环抛异常 → 同；③ 工具抛错 → step 标 error、循环继续。
- **端点**：py_compile + import smoke（项目无 TestClient 约定，与现有 domain 端点一致）。

## 范围（YAGNI）

- **不做**：MCP（Spec 2）/ 问答 `/qa/answer` agent 化（高频不划算）/ 流式 agent（多步返回，与现有 diagnose 一致一次性）/ 多 agent + critic 反思（单 agent 够用）/ 跨请求 agent 记忆。

## 与 Spec 2（MCP）的衔接

`TOOLS` 注册表（name/description/json_schema/async handler）刻意设计成中立接口。Spec 2 直接把这层包装成 MCP server（对外暴露给 Claude Desktop 等客户端），并新增"外部 MCP 工具"（实时负荷/台账）注册进同一注册表供 agent 调用——零返工。
