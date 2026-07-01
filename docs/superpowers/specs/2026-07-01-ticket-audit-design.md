# 两票智能审核 — 设计 spec

> 日期：2026-07-01 ｜ 选型：规则引擎 + LLM 双层（用户确认）
> 状态：待用户审阅

## 背景与目标

现系统只有**两票生成**（`backend/app/routers/domain.py:44` `POST /domain/ticket` → `domain_service.generate_ticket`），缺**审核**已填票据合规性的能力。电网安监核心痛点恰是"审"：已填的操作票/工作票是否缺项、操作顺序是否违安措、危险点是否列出、调度指令编号是否合规。

**目标**：补齐"生成 → 审核"闭环，输入一张已填票据文本，输出结构化审核报告，让人工审核员快速定位问题。

## 架构：规则引擎 + LLM 双层

```
票据文本
  │
  ▼
[结构化解析]  →  {ticket_type, 任务, 操作步骤[], 安措[], 危险点[], 调度指令号, ...}
  │
  ├─ [层1 规则引擎]  确定性硬规则（可配 YAML）
  │     · required_field：必填项缺失（任务/调度指令号/操作人）
  │     · sequence：操作顺序违反安措（如"挂地线"出现在"验电"前）
  │     · danger_point：高风险操作未列危险点（停电/接地/倒闸）
  │     · dispatch_format：调度指令编号格式校验（正则）
  │     · keyword_blocklist：禁用术语/表述
  │
  └─ [层2 LLM 语义审核]  语义类合规
        · 安措是否覆盖所有危险点
        · 术语是否规范、步骤是否可执行
        · 输出结构化 JSON items
  │
  ▼
[聚合] → {overall, score, items[]}
```

**为什么双层**：规则层确定性、可解释、零成本；LLM 层覆盖语义（安措 vs 危险点覆盖关系），二者互补。规则层失败/缺失时系统仍可用（内置默认规则），LLM 失败时降级只返回规则层结果。

## 组件

### 后端
- `backend/app/services/ticket_audit_service.py`（新）
  - `parse_ticket(text) -> dict`：结构化解析（启发式 + LLM 兜底）
  - `audit_ticket(text, ticket_type) -> dict`：编排双层审核 + 聚合
  - `_rule_check(parsed) -> list[item]`：规则引擎
  - `_llm_check(parsed, ticket_type) -> list[item]`：LLM 语义审核（复用 `get_llm_provider`，temperature=0）
- `backend/data/ticket_rules.yaml`（新，可配）：规则定义，缺失时用 `ticket_audit_service._DEFAULT_RULES`
- `backend/app/routers/domain.py`：新增 `POST /domain/ticket/audit`（admin）
- `backend/app/core/metrics.py`：新增 `TICKET_AUDIT = Counter("grid_ticket_audit_total", ..., ["result"])`（result: pass/warn/fail）

### 前端
- `frontend/src/views/Diagnose.vue`：新增第 4 个 tab「两票审核」
  - 输入区：票据类型下拉（操作票/工作票）+ 大文本框（粘贴票据）+「审核」按钮
  - 结果区：总览（overall 绿/黄/红 + score）+ 问题列表（每条 severity badge + 规则id + 描述 + 修正建议）

## 数据结构

```python
# 审核报告
{
  "overall": "pass" | "warn" | "fail",
  "score": 0-100,            # pass>=85, warn 60-84, fail<60
  "ticketType": "操作票",
  "items": [
    {"layer": "rule|llm", "ruleId": "SEQ_001", "type": "sequence",
     "severity": "critical|major|minor", "msg": "挂地线出现在验电前",
     "suggestion": "应先验电确认无电后再挂地线"}
  ],
  "latencyMs": 1234
}
```

## 错误处理

- LLM 超时/失败：`degraded("ticket_audit_llm", e)`，只返回规则层结果，`overall` 按规则层计
- 规则 YAML 解析失败：用 `_DEFAULT_RULES` 兜底 + warn 日志
- 票据解析为空：返回 `{overall:"fail", items:[{severity:critical, msg:"无法识别票据内容"}]}`

## 测试

- `backend/tests/test_ticket_audit.py`：
  - 规则引擎单测：每类规则各 2 个正反例
  - `parse_ticket`：操作票/工作票各 1 例
  - 端点集成测：mock LLM，验证聚合报告结构
- `backend/data/golden_tickets.json`（新）：10 例标注票据（合规/缺项/顺序错/无危险点），回归门禁

## 范围（YAGNI）

- **不做**：票据 PDF/图片 OCR 识别（只接文本，OCR 已有 `parse_service` 可后续接）
- **不做**：与外部两票系统 API 对接
- **不做**：审核规则的可视化编辑器（YAML 手改即可，后续再做）
- 规则库首版覆盖操作票/工作票两类，扩充靠 YAML
