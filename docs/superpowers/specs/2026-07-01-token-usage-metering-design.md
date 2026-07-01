# per-user token 计量 — 设计 spec（仅计量，先观测）

> 日期：2026-07-01 ｜ 选型：仅计量 + 看板，不做配额拦截（用户确认）
> 状态：待用户审阅

## 背景与目标

现 LLM 调用只有全局 `LLM_CALLS` 计数（`backend/app/core/metrics.py:17`，仅 provider 维度），**无法按用户/租户算用量与成本**。电网多部门共用需先摸清"谁用了多少"，再决定要不要限。

**目标（本期）**：补 per-user/租户 token 计量 + 落库 + 聚合看板。**不做配额拦截**（先观测，下期再决定策略）。

## 架构

```
LLM 调用(chat/stream)
   │
   ▼
provider 层解析 usage（prompt_tokens/completion_tokens/total）← 现有 provider 只返回 str，需改为返回 (str, usage)
   │
   ▼
usage_log 落库（user/tenant/model/tokens/ts）+ LLM_TOKENS 指标（可选，按 model）
   │
   ▼
聚合端点 /system/usage/stats → 前端用量看板
```

## 组件

### 后端
- `backend/app/providers/base.py`：`chat/stream` 返回值由 `str` 改为 `UsageResult(text, usage)`（或 `stream` 走回调传 usage，避免破坏迭代器协议）
  - `chat`：返回 `(text, usage)` 元组；各 provider 从响应解析 `usage`（OpenAI 协议响应自带 `usage` 字段）
  - `stream`：最后一个 chunk 的 `usage`（OpenAI stream_options.include_usage）或结束时回调
- `backend/app/models/usage_log.py`（新）：`UsageLog(user_id, tenant_id, model, prompt_tokens, completion_tokens, total_tokens, cost_cents, ts)`（Alembic 迁移）
- `backend/app/services/usage_service.py`（新）：`record_usage(...)`、`query_usage_stats(group_by, range)`
- `backend/app/services/qa_service.py`：LLM 调用后取 usage → `record_usage`（异步，不阻塞返回）
- `backend/app/core/metrics.py`：新增 `LLM_TOKENS = Counter("grid_llm_tokens_total", ..., ["model", "type"])`（type: prompt/completion）
- `backend/app/routers/system.py`：新增 `GET /system/usage/stats?groupBy=user|tenant|model&days=30`（admin）

### 前端
- `frontend/src/views/Admin.vue`：新增「用量看板」tab
  - 维度切换（按用户/租户/模型）+ 时间范围（7/30/90 天）
  - ECharts：token 趋势线 + 饼图占比 + Top 用户表 + 成本估算（按 model 单价 `data/model_price.yaml`）

## 数据流（计费估算）

```
usage_log.total_tokens × model_price[model] → cost_cents
```
单价表 `backend/data/model_price.yaml`（可配，deepseek/qwen/doubao 各 prompt/completion 单价），缺失模型按 0 估算并 warn。

## 错误处理

- provider 未返回 usage（部分供应商不回传）：`record_usage(tokens=0, note="no_usage")`，不报错；看板显示"部分调用未计量"占比
- usage_log 写入失败：`degraded("usage_log", e)`，不影响主问答
- 单价表缺失：成本列显示 "—"

## 测试

- `backend/tests/test_usage.py`：
  - provider usage 解析单测（mock 各家响应）
  - 聚合查询（按 user/tenant/model group）
- 端点：`/system/usage/stats` 鉴权（仅 admin）+ 分组正确性

## 范围（YAGNI）

- **不做**：配额中间件/超额拦截（用户明确选"仅计量先观测"；下期据数据决定策略）
- **不做**：实时计费/账单导出（只做估算展示）
- **不做**：embedding/rerank 的 token 计量（首版只计 LLM 主调用；embedding 按 `grid_embed_calls_total` 已有次数维度）
- **不做**：按 API key 维度（只到 user/tenant）

## 后续演进（不在本期）

- 据观测数据定配额策略 → 加 `QuotaMiddleware`（spec ④的"超额降级/拒绝"选项）
- 成本中心导出（Excel/对接财务）
