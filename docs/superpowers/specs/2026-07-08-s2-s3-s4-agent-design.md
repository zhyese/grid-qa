# S2/S3/S4 设计：基于 S1 引擎的三个 persona 子项目

- **日期**：2026-07-08
- **状态**：设计已确认（4 决策），TDD 实现
- **地基**：S1 通用 Agent 引擎（`agent_runtime`，已合入 main，见 `2026-07-08-agent-runtime-engine-design.md`）
- **本 spec 覆盖**：S2 问答 Agent / S3 告警处置 / S4 工具审计+权限
- **调研**：codegraph 核实接入点（`qa_service.stream_answer` / `/system/alerts/webhook` / `OperationLog`-`RewriteEvent` 范本 / `User.role`+`tenant_id`）

## 共性

三块都基于 S1 引擎：**加 persona + 入口 + 工具，不改引擎核心**。`diagnose_agent`（persona=diagnose）保持零回归。S4 是横切（审计/权限作用于所有 persona 的工具调用），**先做**，S2/S3 自动获得审计+权限。

**实现顺序**：S4（引擎横切）→ S2（问答）→ S3（告警）。

---

## S4：工具审计 + 权限（tenant + role）

### 目标
agent 每次工具调用可追溯（谁/何时/哪个 persona/调了啥/结果/出错没）；高风险工具按 (tenant, role) 限流。

### 接入点（codegraph 核实）
- `ToolRegistry.run`（agent_runtime.py，S1）—— 加审计钩子 + 权限检查的唯一点。
- `User.role` + `User.tenant_id`（auth_service.register_user 已确认字段）。
- `rewrite_event_service.log`（独立 AsyncSessionLocal bg task 范本）—— 审计照此模式。

### 数据结构
新表 `AgentToolCall`（Alembic 迁移，项目用 Alembic）：
```
id, ts(DateTime default now), persona(str), tool(str), iter(int),
args_json(str, 截断 500), result_summary(str, 截断 500), error(bool),
username(str), tenant(str), role(str), degraded(bool)
```

### 组件
- `backend/app/models/agent_tool_call.py` — SQLAlchemy model。
- `backend/app/services/agent_tool_audit_service.py` — `log_tool_call(...)` bg task（独立 session，采样可选、默认全量）+ `query_tool_calls(page,size,filters)`。
- `agent_runtime.py`：
  - `ToolRegistry.run` 加可选 `ctx={username,tenant,role,persona}` 参数；调用后 fire-and-forget 记审计。
  - `run_agent` 把 ctx 透传给 `registry.run`。
  - 新增 `tool_permissions`（dict: tool→[allowed_roles]，默认 `{"draft_ticket": ["admin"]}`）；`run` 前按 (tenant,role) 检查，拒绝则返回"权限不足"+ `metrics.AGENT_TOOL_DENIED.labels(tool).inc()`，不调 handler。
- `backend/app/routers/system.py` — `/system/agent/tool-calls`（admin，审计列表分页）。
- metrics：`AGENT_TOOL_DENIED`（Counter, labelnames=["tool"]）+ 预注册。

### 测试
- 审计记录：mock run，断言 AgentToolCall 写入（用 in-memory SQLite 或 mock session）。
- 权限：draft_ticket 非 admin → 拒绝 + DENIED 指标；admin → 正常。
- ctx 透传：run_agent 带 ctx → 审计含 username/tenant。

---

## S2：问答 Agent（流式思考链步进）

### 目标
主问答加「深度思考」开关，开启后走 `run_agent(QA_PERSONA)`，**流式推送思考链每一步**（meta→tool_step×N→token→done），用户实时看 AI 查了啥资料。

### 接入点
- `qa_service.stream_answer`（meta/token/done 三段）—— 扩展加 `tool_step` 事件 + agentMode 分支。
- `/qa/answer/stream`（routers/qa.py）—— 加 `agentMode` 参数。
- 前端 `Chat.vue`（主问答）+ `streamAnswer`（api/index.js，SSE 解析）—— 加开关 + tool_step 渲染。
- `Diagnose.vue` 的 `agentSteps` 渲染 —— 复用为共享组件。

### persona
`QA_PERSONA`（agent_personas.py）：
```
name="qa", output_format="text",
allowed_tools=["search_regulation","query_equipment_graph","search_similar_case"],  # 无 draft_ticket
fallback=调 qa_service.answer 原链路（降级）
```

### 数据流（stream_answer agentMode）
```
agentMode=True:
  yield meta(conversationId, sources=[])
  result = await run_agent_stream(db, QA_PERSONA, query, on_step=lambda s: yield tool_step(s))  # 每步回调推 tool_step
  yield token(result.answer)  # 最终答案（一次性或分片）
  yield done(iterations, degraded, toolsUsed)
agentMode=False: 原流式链路不变
```
> `run_agent` 需支持 `on_step` 回调（每完成一个工具步调用），用于流式推送。非流式调用不传 on_step（行为不变，diagnose 零回归）。

### 组件
- `agent_personas.py`：加 `QA_PERSONA` + `_qa_fallback`。
- `agent_runtime.py`：`run_agent` 加 `on_step: Callable|None` 参数（每步后回调；默认 None，行为不变）。
- `qa_service.py`：`stream_answer` 加 `agent_mode` 参数 + agentMode 分支。
- `routers/qa.py`：`/qa/answer/stream` 加 `agentMode`（body 字段）。
- 前端 `Chat.vue` + `api/index.js streamAnswer`：开关 + tool_step 事件渲染（复用 Diagnose 思考链样式）。

### 测试
- `run_agent` on_step：mock provider，断言每步触发回调、steps 内容。
- stream_answer agentMode：mock run_agent，断言事件序列 meta→tool_step→token→done。
- QA_PERSONA 配置 + fallback。
- `/qa/answer/stream` agentMode 参数透传（py_compile+import smoke）。

---

## S3：告警自动处置

### 目标
告警进来后自动触发 `ALERT_PERSONA` 分析 → 生成「诊断+处置建议+操作票草案」→ 存 `AlertDisposal` 表供管理员看。

### 接入点
- `/system/alerts/webhook`（system.py，现有 Grafana 回调，只记录）—— 收到告警后 bg task 触发 `ALERT_PERSONA`。
- 新 `/system/alerts/dispose`（手动触发：传告警文本，方便演示/测试）。
- `OperationLog`（现有，告警记录）—— 复用查告警列表。

### 数据结构
新表 `AlertDisposal`（Alembic）：
```
id, created_at, severity(str), title(str), summary(str, 告警原文摘要),
diagnosis_json(text), handling(str), ticket_draft_json(text),
status(str: pending/disposed), persona="alert", source(str: webhook/manual)
```

### persona
`ALERT_PERSONA`（agent_personas.py）：
```
name="alert", output_format="json",
allowed_tools=["search_regulation","query_equipment_graph","search_similar_case","draft_ticket"],
fallback=模板化处置（{summary:"自动处置失败，请人工",causes:[]})
```

### 数据流
```
告警(webhook 或 /dispose 手动) → 写 AlertDisposal(status=pending)
  → bg task: run_agent(ALERT_PERSONA, 告警文本) → 拆 diagnosis/handling/ticket_draft
  → 更新 AlertDisposal(status=disposed) + 指标
```

### 组件
- `backend/app/models/alert_disposal.py` — model。
- `backend/app/services/alert_disposal_service.py` — `trigger_disposal(severity,title,summary,source)`（写 pending + bg task 跑 agent + 更新）+ `list_disposals(page,size)`。
- `agent_personas.py`：加 `ALERT_PERSONA` + `_alert_fallback`。
- `routers/system.py`：`/system/alerts/webhook` 末尾加 `trigger_disposal(...)`（bg task，不阻塞 webhook 响应）；新 `/system/alerts/dispose`（admin，手动触发）+ `/system/alerts/disposals`（admin，列表）。
- 前端 `Admin.vue`：告警面板加「自动处置」视图（告警→诊断→处置→操作票草案）。
- metrics：沿用 `AGENT_CALLS.labels("alert")`。

### 测试
- ALERT_PERSONA 配置 + fallback。
- trigger_disposal：mock run_agent，断言 AlertDisposal pending→disposed、字段填充。
- /dispose 端点 smoke。
- webhook 触发 disposal（mock trigger_disposal，断言被调）。

---

## 不变性约束（全局）
- **不改 S1 引擎核心逻辑**：`run_agent` 只加可选参数（`on_step`/`ctx`），默认行为不变 → `diagnose_agent` 仍零回归（黄金回归测试须持续通过）。
- 三块各自独立可测、可 commit。
- 新表用 Alembic 迁移（项目既有 Alembic）。
- 测试用 `asyncio.run` + `PYTHONPATH=backend ./venv/Scripts/python.exe -m pytest`（见 [[agent-runtime-engine]] 坑）。
- commit 用 PIPESTATUS 检测 pytest 退出码（管道陷阱）。

## 风险
- `run_agent` 加 `on_step`/`ctx` 参数须保证默认 None 时 diagnose 行为不变（黄金回归护栏）。
- S4 审计 bg task 用独立 session（仿 rewrite_event，避免 session 并发 500 教训）。
- S2 流式 `on_step` 回调里 yield 需在 async generator 上下文，注意不能在回调直接 yield（要通过队列/事件桥接）—— 实现时用 `asyncio.Queue` 在 stream_answer 里消费。
- S3 webhook bg task 触发 agent 耗时，须真正后台（`asyncio.create_task`，不阻塞 webhook 200 响应）。
