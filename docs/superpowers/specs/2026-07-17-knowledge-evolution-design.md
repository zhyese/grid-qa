# 知识库自进化闭环 · 设计文档

- 日期：2026-07-17
- 范围：P1（手动+草稿+审核台）+ P2（回流 Milvus）+ P3（定时自动+配额+监控）全量
- 决策：零依赖贪心近邻聚类；新建 `KnowledgeEvolutionDraft` 表
- 依赖：复用 `903a71b` 的 PersistentTask 队列 + worker + governance 审核范式

## 1. 背景与目标

项目已具备 `evidence_gap_service`（证据补全，含 `ai_draft` 草稿能力）和 `feedback_optimizer_service`（dislike 黑名单），但两者散落，缺乏全自动闭环。

目标闭环：对 Dislike 问答记录 **定时异步聚类 → 识别 Milvus 高频盲区 → 结合最近规程文档 LLM 编写增量 chunk 草稿 → 审核机制一键批准回流知识库**。

## 2. 现状与缺口（事实）

| 环节 | 现状 | 缺口 |
|---|---|---|
| 采集 | `Feedback` + `EvidenceGap.collect()` + `/qa/feedback` dislike 入口 | — |
| 聚类 | 无 | **dislike 向量聚类 → 高频簇** |
| 盲区识别 | `knowledge_quality_service` 有质量统计基础 | **簇 → Milvus 检索 → 盲区判定** |
| 草稿生成 | `evidence_gap_service.ai_draft()` 单条草稿 | **结合规程文档批量写增量 chunk** |
| 审核回流 | governance `Issue+Review` 范式 + `chunk_service` 入库入口 | **把审核流接到草稿→Milvus 回流** |
| 调度 | `PersistentTask` + worker + task_center | — |

结论：70% 零件已备齐，缺一根编排线 `knowledge_evolution_service.py`。

## 3. 架构设计

```
新增编排器 services/knowledge_evolution_service.py
   ├─ 复刻 knowledge_governance 的 scan 入队范式（queue='evolution'）
   └─ 复刻 review 审核范式（draft → approved → indexed）
复用：PersistentTask + worker / embedding_service / feedback_service
      / chunk_service + document_service / retrieval_service / knowledge_quality_service
新增表：KnowledgeEvolutionDraft（复刻 KnowledgeGovernanceIssue 范式）
```

## 4. 数据模型 `KnowledgeEvolutionDraft`

SQLAlchemy 模型（对齐项目现有 model 风格，create_all 建表）：

```python
class KnowledgeEvolutionDraft(Base):
    __tablename__ = "knowledge_evolution_draft"
    id            = Column(String(36), primary_key=True)          # uuid
    tenant_id     = Column(String(64), index=True, default="default")
    cluster_id    = Column(String(64), index=True)                # 聚类簇 ID
    representative_query = Column(String(500))                     # 簇代表问题
    member_queries_json  = Column(Text)                            # 簇内 dislike 原始问题(审计)
    gap_evidence_json    = Column(Text)                            # {top1_score, hit_doc_ids, confidence}
    source_doc_ids_json  = Column(Text)                            # 草稿参考的规程文档
    draft_title   = Column(String(256))
    draft_content = Column(Text)                                   # 结构化 chunk 正文
    status        = Column(String(16), index=True, default="draft") # draft|approved|indexed|rejected|withdrawn
    chunk_id      = Column(String(64), default="")                 # 回流后 Chunk.id(撤回入口)
    quality_score = Column(Float, default=0.6)                     # AI 生成 < 人工 1.0,检索降权
    model_type    = Column(String(32), default="")
    reviewer      = Column(String(64), default="")
    review_note   = Column(String(500), default="")
    reviewed_at   = Column(DateTime, nullable=True)
    created_at    = Column(DateTime, default=utcnow)
    indexed_at    = Column(DateTime, nullable=True)
```

状态机：`draft → approved → indexed`；`draft/approved → rejected`；`indexed → withdrawn`（撤回删 chunk）。

## 5. 服务接口（knowledge_evolution_service.py 函数签名）

```python
# —— 触发（复刻 governance.enqueue_governance_scan / run_scan）——
async def enqueue_evolution_scan(tenant_id, *, since_hours=168, model_type=None) -> dict | None
async def run_scan(db, tenant_id, *, since_hours=168, model_type=None) -> dict

# —— 管道四段 ——
async def _extract_dislike(db, tenant, since_hours) -> list[dict]      # Feedback(dislike)+EvidenceGap(pending) 去重
def _cluster(items, threshold=0.82, min_size=3) -> list[Cluster]        # 零依赖贪心近邻
async def _identify_blind_spot(db, cluster) -> dict | None              # 检索 top1_score<0.55 = 盲区
async def _generate_draft(db, cluster, evidence, model_type) -> dict    # LLM + 规程文档 RAG 增强

# —— 落库 / 审核 / 回流 / 撤回 ——
async def _persist_drafts(db, tenant, drafts) -> list[KnowledgeEvolutionDraft]
async def list_drafts(db, tenant, *, status="", page=1, size=20) -> dict
async def get_draft(db, draft_id, tenant) -> dict | None
async def review_draft(db, draft_id, tenant, *, action, note, reviewer) -> dict   # action: approve|reject
async def reflow_to_kb(db, draft) -> str                                  # → chunk_id（调 chunk_service）
async def withdraw_draft(db, draft_id, tenant) -> dict                    # 删 chunk + Milvus delete
async def get_stats(db, tenant) -> dict
```

## 6. 五阶段管道详细

### 6.1 抽取
`feedback_service` 拉 `since_hours`（默认 168=7天）内 `dislike` 记录，并并入 `EvidenceGap.status=pending`，按 query 归一化去重。

### 6.2 聚类（零依赖贪心近邻）
```python
def _cluster(items, threshold=0.82, min_size=3):
    clusters = []
    for it in items:                          # it = {query, vec}
        placed = False
        for c in clusters:
            if cosine(it["vec"], c["centroid"]) >= threshold:
                c["members"].append(it)
                c["centroid"] = mean_vec(c["members"])   # 增量更新质心
                placed = True; break
        if not placed:
            clusters.append({"centroid": it["vec"], "members": [it]})
    return [c for c in clusters if len(c["members"]) >= min_size]
```
向量来自 `embedding_service.embed_texts(queries)`（批量，带 provider 缓存）。

### 6.3 盲区判定
对每个簇取 `representative_query`（质心最近成员）→ `retrieval_service` 检索 top-1 → 若 `score < 0.55`（或 confidence ∈ {refused, medium}）→ 确认盲区，记录 `gap_evidence`。

### 6.4 草稿生成（LLM + RAG 增强，防胡编）
- 取参考文档：`Document` 表 `created_at desc` + 向量检索"规程/标准"类 top-3（`source_doc_ids`）
- Prompt 模板：
```
你是电网运维知识工程师。基于以下高频用户疑问（系统未能很好回答）和参考资料，
编写一条结构化知识条目。必须严格基于参考资料，不得编造，标注来源。

【用户疑问簇】{member_queries}
【参考资料】{source_docs}

输出 JSON：
{"title":"...", "content":{"现象":"...","原因":"...","处置":"...","依据":"..."}, "source_refs":["doc_id..."]}
```

### 6.5 回流（P2）
`approved` → `reflow_to_kb(draft)` → 调 `chunk_service`/`document_service` 现有入库函数：
`embed(draft_content) → Milvus insert + MySQL Chunk`，metadata 打标 `source_type='ai_evolution'`、`quality_score=0.6`、`draft_id`。
→ `status=indexed`，`chunk_id=<new>`，`indexed_at=now`。

> 注：`chunk_service`/`document_service` 的精确入库函数签名在 writing-plans 阶段用 codegraph 定位确认后接入，不在本 spec 编造。

## 7. 触发与调度（P3 全量）

- 手动：`POST /knowledge-evolution/scan` → `enqueue_evolution_scan`（返回 task=异步 / None=同步）
- 定时：周期性 `enqueue_task_record(queue='evolution', task_type='knowledge_evolution.scan', run_after=下次周期)`，worker 认领；周期默认每天（可配 `KNOWLEDGE_EVOLUTION_CRON_HOURS`，默认 24）
- `tasks/builtin.py` 注册 `knowledge_evolution.scan` handler → `run_scan`
- 配额：每周 `indexed` ≤ N（默认 20，`KNOWLEDGE_EVOLUTION_WEEKLY_QUOTA`），超配仅产 draft 不回流

## 8. 检索降权（防回环污染）

`retrieval_service` 检索后，对 `source_type='ai_evolution'` 的 chunk 分数乘 `quality_score`(0.6)；可由 `AI_EVOLUTION_RETRIEVAL_FILTER`（默认 `downgrade`，可选 `exclude`）控制。

## 9. 监控指标（core/metrics.py）

| 指标 | 类型 | 标签 |
|---|---|---|
| `evolution_clusters` | Gauge | tenant |
| `evolution_drafts_total` | Counter | tenant, status |
| `evolution_indexed_total` | Counter | tenant |
| `evolution_rejected_total` | Counter | tenant |
| `evolution_scan_duration_seconds` | Histogram | tenant |

## 10. API 端点（routers/knowledge_evolution.py，前缀 `/knowledge-evolution`）

```
POST /scan                 手动触发入队   (DOC_MANAGE)
GET  /scan/{task_id}       查扫描状态     (DOC_MANAGE)
GET  /drafts               草稿列表(分页+status) (DOC_READ)
GET  /drafts/{id}          草稿详情       (DOC_READ)
POST /drafts/{id}/review   审核(approve/reject+note) (DOC_MANAGE)
POST /drafts/{id}/withdraw 撤回(删chunk+Milvus)     (DOC_MANAGE)
GET  /stats                统计           (DOC_READ)
```
权限复用 `DOC_READ`/`DOC_MANAGE`，走 `require_perm`。

## 11. 前端

- `views/KnowledgeEvolution.vue`：草稿审核台（列表 + 详情 + 一键 approve/reject/withdraw + 手动 scan 按钮 + 配额进度）
- `api/index.js`：对应 7 个端点封装
- `router/index.js`：`/knowledge-evolution` 路由 + 侧栏入口

## 12. 风险与对策

| 风险 | 对策 |
|---|---|
| 回环污染 | `source_type='ai_evolution'` 打标 + quality_score 降权 + 检索可过滤/排除 |
| 胡编内容 | RAG 绑规程文档 + prompt 禁编造 + 审核必经 |
| 重复回流 | representative_query hash 作 idempotency_key；已 indexed 簇不再生成 |
| 成本 | 仅高频簇(≥3)生成 + 草稿走便宜模型 |
| 撤回 | withdrawn → 删 Chunk + Milvus delete(chunk_id) |

## 13. 测试策略（tests/test_knowledge_evolution.py）

- 聚类正确性（已知向量→期望簇划分）
- 盲区判定（mock retrieval score）
- 草稿生成（mock LLM 返回 JSON）
- 审核回流幂等（重复 approve 不重复入 chunk）
- 撤回（删 chunk + Milvus delete 被调用）
- 配额（超配不回流）
- tenant 隔离
- 定时入队（enqueue 幂等）

## 14. 实施分阶段

| 阶段 | 范围 | 交付 |
|---|---|---|
| P1 | 模型+服务核心(extract/cluster/identify/generate)+手动 scan+审核台(不回流) | 链路跑通，验证草稿质量 |
| P2 | reflow_to_kb + withdraw + source_type 降权 | 真正补知识 + 可撤回 |
| P3 | 定时 PersistentTask + 配额 + 指标 + 前端 | 全自动闭环 |

## 15. 文件清单

```
新增 backend/app/models/knowledge_evolution.py
新增 backend/app/schemas/knowledge_evolution.py
新增 backend/app/services/knowledge_evolution_service.py
新增 backend/app/routers/knowledge_evolution.py
改  backend/app/tasks/builtin.py            注册 knowledge_evolution.scan
改  backend/app/services/retrieval_service.py source_type 降权
改  backend/app/core/metrics.py             5 个指标
改  backend/app/db/init_db.py               注册新模型 create_all
改  backend/app/main.py                      挂载 router
新增 tests/test_knowledge_evolution.py
新增 frontend/src/views/KnowledgeEvolution.vue
改  frontend/src/api/index.js + router/index.js
```
