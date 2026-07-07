# 证据补全闭环设计

> 2026-07-07 · brainstorming 产出 · 待 review

## 1. 背景

CRAG 自纠错（`_crag_correct` → `confidence_of`）已能判 confidence=medium（证据有限）/refused（证据不足），前端 `confLabel` 也展示了。但 **medium/refused 的 bad case 直接流失**——没有落库、没有人工兜底入口、没有补全后回流知识库。同一个证据不足的问题，下一个用户问还是证据不足。

证据补全闭环：把 medium/refused 的问题**收集 → AI 续写草稿 → 人工确认 → 同步入库（文档+缓存）**，下次同问题直接命中（缓存秒回 / 检索命中新 FAQ 文档），不再证据有限/不足。

## 2. 目标

- **收集**：confidence∈{medium,refused} 的问答自动落 `evidence_gap` 表（去重）+ Chat 提供主动上报入口
- **补全**：AI 续写出草稿（放宽检索再生成）→ 管理员审核编辑确认
- **回流**：确认后同步——作 FAQ 文档 vectorize（检索命中相似问题）+ 写 qa_cache/Redis（原 query 秒回），confidence 标 high
- **闭环**：下次同 query 命中缓存或检索，不再 medium/refused

## 3. 非目标

- 不改 CRAG 分级逻辑（confidence 来源不变）
- 不做自动 AI 入库（AI 草稿必经人工确认，防 AI 又编造）
- 不做多租户隔离的 evidence_gap（默认租户共享，按需扩展）

## 4. 数据模型（新表 `evidence_gap`）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| ts | datetime | 收集时间 |
| query | text | 归一化 nq（去重键）|
| original_answer | text | 当时证据有限/不足的答案 |
| confidence | varchar(16) | medium / refused |
| grade | varchar(16) | crag grade（ambiguous/incorrect）|
| crag_action | varchar(16) | normal/refused/self_rag_skip |
| source | varchar(16) | auto（自动收集）/ manual（Chat 上报）|
| status | varchar(16) | pending→ai_drafted→confirmed→synced / ignored |
| ai_draft | text | AI 续写草稿（null 未生成）|
| final_answer | text | 人工确认的最终答案（null 未确认）|
| synced_doc_id | varchar(64) | 同步到 document 表的 doc_id（null 未同步）|
| synced_cache | int | 是否已写 qa_cache/Redis（0/1）|
| tenant | varchar(32) | 租户 |
| operator | varchar(64) | 操作管理员（确认/同步人）|
| handled_at | datetime | 确认时间 |

**去重键**：`(query, status='pending')`——同 nq 已有 pending 不重复写。

## 5. 组件（接现有设施，codegraph 核实）

### 5.1 EvidenceGapCollector（自动收集）
- **触发**：`qa_service.answer/stream` 末尾，if `confidence in ("medium","refused")` and `EVIDENCE_GAP_AUTO_COLLECT`
- **去重**：查 `evidence_gap` 同 query 且 status=pending 已存在则跳过
- **写表**：独立 `AsyncSessionLocal`（bg task，防 session 并发 500）
- **依赖**：`_crag_correct` 的 confidence 返回值

### 5.2 EvidenceGapReporter（主动上报）
- **触发**：Chat 对 medium/refused 答案点「上报证据不足」
- **接口**：`POST /qa/evidence-gap/report`（user，body=query/answer/confidence）
- **去重**：同 Collector

### 5.3 AIDrafter（AI 续写草稿）
- **触发**：管理员在 evidence_gap 列点「AI 续写」
- **策略**：放宽检索（topk×2、临时降 CRAG 阈值）再 LLM 生成 → 草稿存 `ai_draft`
- **依赖**：`mixed_search`（放宽参数）+ `get_llm_provider`
- **状态**：pending → ai_drafted

### 5.4 人工确认
- 管理员编辑 `final_answer`（基于 ai_draft 或重写）→ status=confirmed

### 5.5 SyncService（同步入库）
- **触发**：管理员「确认同步」时自动触发（confirm 接口一站式：编辑 final_answer + 同步入库，不分两步）
- **文档入库**：`document_service` 创建一条 FAQ 文档（docType="证据补全FAQ"）→ vectorize（切块+Milvus+BM25）
- **缓存双写**：`cache_set_mysql`（confidence=high）+ `cache_set_json`（Redis，key=qa:{model}:{nq}）
- **状态**：confirmed → synced（记 synced_doc_id + synced_cache=1）
- **依赖**：`document_service.vectorize` + `cache_persist.cache_set_mysql` + `redis_client.cache_set_json`

## 6. 数据流（状态流转）

```
用户问 → answer/stream → confidence=medium/refused
  ├─[自动] Collector 去重写 evidence_gap(status=pending, source=auto)
  └─[主动] Chat「上报」→ Reporter 写(pending, source=manual)

管理员（Admin「📝 证据补全」tab）:
  pending 列表 → 点「AI续写」→ AIDrafter 生成草稿 → ai_drafted
  → 编辑 final_answer → 「确认同步」→ SyncService:
       ├─ 文档入库（FAQ docType, vectorize）
       └─ 缓存双写（qa_cache high + Redis）
     → status=synced

下次同 query:
  ├─ 缓存命中（Redis/MySQL，confidence=high）→ 秒回，不再 medium/refused
  └─ 检索命中（新 FAQ 文档）→ 有证据，confidence=high
```

## 7. 接口

### system router（admin）
- `GET /system/evidence-gap?status=&page=&size=` — 列表（按 status 过滤）
- `POST /system/evidence-gap/{id}/ai-draft` — AI 续写草稿
- `POST /system/evidence-gap/{id}/confirm` — 人工确认（body=final_answer，触发同步入库）
- `POST /system/evidence-gap/{id}/ignore` — 忽略（status=ignored，不补）
- `DELETE /system/evidence-gap/{id}` — 删除

### qa router（user）
- `POST /qa/evidence-gap/report` — 主动上报（body=query/answer/confidence）

## 8. 前端

### Admin 新 tab「📝 证据补全」
- 状态过滤（pending/ai_drafted/confirmed/synced/ignored）+ 列表
- 每条：query / original_answer / confidence badge / 状态
- 操作按钮：AI续写（pending）/ 编辑确认+同步（ai_drafted）/ 查看（synced）
- 编辑弹窗：ai_draft 预填 → 管理员编辑 final_answer → 确认同步

### Chat（medium/refused 答案）
- 答案下加「上报证据不足」按钮（仅 confidence=medium/refused 显示）
- 点击 → POST /qa/evidence-gap/report → toast「已上报，将补充证据」

## 9. 错误处理（全走 `degraded`）

- Collector 写表失败 → `degraded("evidence_gap_collect")`，不阻塞问答
- AI 续写失败 → `degraded("evidence_gap_ai_draft")`，草稿空，管理员手写
- 同步入库失败（vectorize/cache）→ `degraded("evidence_gap_sync")`，status 保持 confirmed，可重试

## 10. 测试（TDD）

- `test_evidence_gap_collector`：mock confidence=medium → 写表；同 query 去重
- `test_evidence_gap_reporter`：上报接口
- `test_ai_drafter`：mock LLM → 生成草稿
- `test_sync_service`：mock document_service/cache → 双写 + 状态 synced
- `test_evidence_gap_api`：admin 接口（401/200 + 状态流转）
- 集成：端到端（medium → 收集 → AI → 确认 → 同步 → 下次命中缓存）

## 11. 配置（config.py 新增）

```python
EVIDENCE_GAP_AUTO_COLLECT: bool = True   # 自动收集 medium/refused
EVIDENCE_GAP_DRAFT_TOPK_MULT: int = 2    # AI 续写检索放宽倍数
EVIDENCE_GAP_FAQ_DOCTYPE: str = "证据补全FAQ"  # 同步入库的 docType
```

## 12. 文件清单

**新建**：
- `backend/app/models/evidence_gap.py`（表 model）
- `backend/app/services/evidence_gap_service.py`（Collector/Reporter/AIDrafter/SyncService + 查询）
- `backend/app/routers/`：system 加 evidence-gap 接口 / qa 加 report 接口
- `backend/migrations/versions/xxx_add_evidence_gap.py`（Alembic）
- `frontend/src/views/Admin.vue`（新 tab「📝 证据补全」）
- `frontend/src/views/Chat.vue`（medium/refused 加上报按钮）
- `tests/test_evidence_gap*.py`

**修改**：
- `backend/app/services/qa_service.py`（answer/stream 末尾接 Collector）
- `backend/app/config.py`（3 字段）

## 13. 验收标准

1. medium/refused 问答自动落 evidence_gap（去重，自动收集开关可控）
2. Chat 可主动上报（按钮仅 medium/refused 显示）
3. 管理员 AI 续写 → 草稿生成；编辑确认 → 同步入库（文档 vectorize + 缓存双写）
4. 同步后下次同 query：缓存命中（cached=true, confidence=high）或检索命中新 FAQ 文档
5. 不再返回 medium/refused（该 query 补全后）
6. 全链路降级可见（degraded 指标）
