# 数据飞轮 · 实现计划（bite-sized TDD）

- **spec**：`docs/superpowers/specs/2026-07-20-data-flywheel.md`（`6fb5bf5`）
- **起点 BASE**：`6fb5bf5`
- **硬约束**：全 opt-in 默认关；前端最小改动；每 task 独立 commit；测试先行；显式 git add；main 分支
- **复用范式**：`domain_event`/`persistent_task`/`evidence_gap`/`_bg_tasks`，零新底座

---

## Batch A · P0 治理联动（断点 A，最高 ROI）

### Task A1 · quality_event 表 + 总线骨架
- **测试** `tests/test_quality_event_bus.py`：`emit(source="x",type="y",payload={})` 入库；`subscribe("x.*", handler)` 注册后 emit 触发 handler（mock 计数）；handler 异常不阻塞 emit（degraded）。
- **文件**：`models/quality_event.py`（新表 QualityEvent: id/source/type/payload json/status/created_at/handled_at）+ `services/quality_event_bus.py`（`emit/subscribe/_dispatch`，_bg_tasks 派发）+ Alembic + init_db._TABLE_MIGRATIONS + `config.QUALITY_BUS_ENABLE`(默认关)
- **验证**：pytest 绿；关开关 emit 仅入库不派发（兼容）。
- **commit**：`feat(data-flywheel): A1 质量事件总线骨架(quality_event表+emit/subscribe,opt-in)`

### Task A2 · 治理状态变更 emit
- **测试**：mock `knowledge_governance` withdraw/supersede/expire 端点 → 断言 `quality_event_bus.emit(source="governance",type="doc_blocked")` 被调。
- **文件**：`routers/knowledge_governance.py`（withdraw/supersede/expire 处理后 emit）+ `services/knowledge_governance_service.py`（状态流转钩子）
- **验证**：pytest 绿；端到端 withdraw 一个 doc → quality_event 新行。
- **commit**：`feat(data-flywheel): A2 治理状态变更emit(doc_blocked事件)`

### Task A3 · 治理联动订阅·Milvus 软删
- **测试**：`governance_propagate_handler(doc_blocked)` → 断言 Milvus 该 doc_id 向量被删/标记。
- **文件**：`services/governance_propagate_service.py`（新，subscribe handler）+ `clients/milvus_client.py`（补 `delete_by_filter(collection, expr)`，无则加；软删优先标 status 字段）+ `config.GOVERNANCE_PROPAGATE_ENABLE`(默认关)
- **验证**：pytest 绿；容器内 withdraw doc → `docker exec backend python -c "milvus 查 doc_id 计数"` 归零。
- **commit**：`feat(data-flywheel): A3 治理联动Milvus软删(GOVERNANCE_PROPAGATE_ENABLE,opt-in)`

### Task A4 · 治理联动订阅·Neo4j + qa_cache 反向失效
- **测试**：handler → 断言 Neo4j 该 doc 边清（复用 delete_document 清 kg 逻辑）+ qa_cache 含该 docId 的行删 + Redis L1 同 key 失效。
- **文件**：`services/governance_propagate_service.py`（扩 handler：Neo4j + qa_cache 扫 `data->retrievalSource`）+ 复用 `kg_service` 清理 + `cache_persist` 反向删
- **验证**：pytest 绿。
- **commit**：`feat(data-flywheel): A4 治理联动Neo4j+qa_cache反向失效`

### Task A5 · cv 加治理版本段 + semantic_cache 查治理
- **测试**：① `citation_cache_version()` 含 G 段，治理 bump 后 G+1；② `semantic_cache_get` 命中 blocked doc 的答案 → 降级 miss。
- **文件**：`config.citation_cache_version`（加 G=`qa:gov_gen` Redis 计数器）+ `rag/semantic_cache.py`（命中后过 `blocked_document_ids`）+ `quality_event_bus`（governance 事件 bump G）+ `config.SEMANTIC_CACHE_GOV_FILTER_ENABLE`(默认关)
- **验证**：pytest 绿；withdraw 后同 query semantic 旧答案被过滤。
- **commit**：`feat(data-flywheel): A5 cv加治理版本段+semantic查治理(断点A收口)`

---

## Batch B · P1 总线打通（断点 E + B）

### Task B1 · dislike emit quality_event
- **测试**：`routers/qa.py::feedback` dislike + retrieval_quality in (poor,None) → 断言 `emit(source="feedback",type="dislike")`。
- **文件**：`routers/qa.py::feedback`（emit）+ `config.DISLIKE_TO_GAP_ENABLE`(默认关)
- **验证**：pytest 绿。
- **commit**：`feat(data-flywheel): B1 dislike emit质量事件(DISLIKE_TO_GAP_ENABLE)`

### Task B2 · evidence_gap 订阅 dislike
- **测试**：`subscribe("feedback.dislike", evidence_gap_handler)` → emit dislike → 断言 `evidence_gap.collect` 被调 + 新增行。
- **文件**：`services/evidence_gap_service.py`（注册订阅 handler）+ `services/quality_event_bus.py`（启动注册）
- **验证**：pytest 绿；端到端 dislike → evidence_gap 表新行。
- **commit**：`feat(data-flywheel): B2 evidence_gap订阅dislike(断点B收口)`

### Task B3 · online_eval/retrieval_eval emit
- **测试**：`online_eval.eval_quality` faithfulness < FAITHFULNESS_GATE → emit(source="online_eval",type="low_faith")；golden recall<92% → emit(source="retrieval_eval",type="eval_low")。
- **文件**：`services/online_eval_service.py` + `services/retrieval_eval_service.py`（emit）+ `config.EVAL_EMIT_ENABLE`(默认关)
- **验证**：pytest 绿。
- **commit**：`feat(data-flywheel): B3 评测低分emit质量事件`

---

## Batch C · P2 评测驱动 + 上传 + 度量收口（断点 C + D）

### Task C1 · retrieval_tune 订阅 eval_low
- **测试**：emit eval_low → 断言 `retrieval_tune_service.scan` 被调（只建议模式）。
- **文件**：`services/retrieval_tune_service.py`（订阅 handler）+ `config.EVAL_TO_TUNE_ENABLE`(默认关)
- **验证**：pytest 绿。
- **commit**：`feat(data-flywheel): C1 retrieval_tune订阅eval_low(断点C收口)`

### Task C2 · 上传引导治理元数据
- **测试**：`upload(effectiveAt=...,expiresAt=...,versionOf=...)` → 断言 `KnowledgeDocumentMetadata` 建行（status=draft/active）。
- **文件**：`routers/document.py::upload`（加 Form 字段）+ `services/document_service.py::upload_documents`（建 meta）+ `frontend/src/views/Documents.vue`（上传卡加可选治理字段，opt-in 不阻断）+ `config.GOVERNANCE_UPLOAD_REQUIRE`(默认关)
- **验证**：pytest 绿；前端 build 通过。
- **commit**：`feat(data-flywheel): C2 上传引导治理元数据(断点D收口)`

### Task C3 · 飞轮度量指标 + Grafana 面板
- **测试**：5 指标注册 + 触发路径埋点（`grid_governance_propagated_total` / `grid_quality_event_total` / `grid_feedback_fix_rate` / `grid_faithfulness_trend` / `grid_kb_freshness`）。
- **文件**：`core/metrics.py`（5 指标）+ 各订阅 handler 埋点 + `monitoring/rag_grafana-data`（新面板 json，4-5 图）+ init_metric_series 预注册
- **验证**：`/metrics` 含新指标；Grafana 面板渲染。
- **commit**：`feat(data-flywheel): C3 飞轮度量5指标+Grafana面板`

### Task C4 · 全链路回归 + 文档
- **测试**：端到端 dislike→总线→evidence_gap→回流；withdraw→联动清理→cv bump；online_eval 低分→tune。
- **文件**：`tests/test_data_flywheel_integration.py`（集成）+ 更新 `.superpowers/sdd/progress.md` + `docs/系统架构.md`（图7 补质量事件总线）
- **验证**：集成测试绿；全量回归。
- **commit**：`test(data-flywheel): C4 全链路集成回归+文档同步`

---

## 任务依赖与顺序

```
A1(总线骨架) ──> A2(governance emit) ──> A3(Milvus订阅) ──> A4(Neo4j+cache订阅) ──> A5(cv+semantic)
A1 ──> B1(dislike emit) ──> B2(gap订阅dislike)
A1 ──> B3(eval emit) ──> C1(tune订阅eval)
                                    C2(上传元数据,独立)
C3(度量) <── 依赖 A3/B2/C1 埋点
C4(集成回归) <── 依赖全部
```

**建议执行序**：A1 → A2 → A3 → A4 → A5（Batch A 收口断点 A）→ B1 → B2 → B3 → C1 → C2 → C3 → C4。

每 task：失败测试 → 实现 → 回归 → 显式 git add + commit。

---

## 进度账本（实施时更新 `.superpowers/sdd/progress.md`）

- [ ] A1 / A2 / A3 / A4 / A5
- [ ] B1 / B2 / B3
- [ ] C1 / C2 / C3 / C4
