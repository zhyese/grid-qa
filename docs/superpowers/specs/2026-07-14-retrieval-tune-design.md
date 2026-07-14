# 检索质量自动调参建议（只建议模式）

- **日期**: 2026-07-14
- **状态**: 待用户复核
- **关联**: 打通 `eval_retrieval` / `feedback_optimizer` / `knowledge_quality` 三座孤岛成"评测→扫描→建议"闭环；**只建议不自动应用**
- **与①协同**: 扫描触发可复用①异步队列的 `default` 队列（若①已落地）；独立可用（admin 按钮）

---

## 1. 背景与动机

检索调优现状是"三座孤岛没打通"：

| 已有 | 现状 | 缺口 |
|---|---|---|
| `scripts/eval_retrieval.py` | 离线**手动**跑 golden，出 markdown | 只评不调；绕 HTTP；未 service 化 |
| `feedback_optimizer_service` | 分析 dislike → 调**缓存** TTL/黑名单 | 不调检索参数（RRF/MMR/rerank/topk） |
| `knowledge_quality_service` | 知识库质量评分（覆盖度/盲区） | 不评检索效果 |
| `rewrite_evaluator` | 改写前后 A/B 轻量对比范式 | 未泛化到参数调优 |

**用户决策：只建议模式** → 不需要参数热调（Redis runtime）、不需要应用/回滚机制。聚焦三件事：**评测 service 化 + 参数扫描建议引擎 + 报告/Admin tab**。参数最终人工改 `.env` 重启。

## 2. 目标与非目标

**目标**
- **G1 评测 service 化**：直接调 `mixed_search`（不绕 HTTP），算 recall@k / MRR / nDCG / 无结果率。
- **G2 参数扫描引擎**：单参数扰动 + 开关 A/B，产出可解释建议（"改 RRF_K 60→40，recall +3.1%"）。
- **G3 报告 + Admin tab**：复用 feedback_optimizer 报告范式 + 「复制 .env 行」闭环抓手。

**非目标**
- 不自动应用参数（只建议，人工改 `.env` 重启）。
- 不做参数热调（只建议无需在线生效）。
- 不做坐标下降 / 网格 / 贝叶斯优化（golden 12 条小样本，过拟合风险；扰动够用）。

## 3. 现状（可复用资产）

- `backend/data/golden_qa.json`（12 条，`expect[]` 二值相关 + `relevant_docs` 分级 1-3）+ `validate_golden.py` CI 门禁
- `doc_relevance_binary`（`eval_retrieval.py` 现有，提到 service 复用）
- `feedback_optimizer_service` 报告缓存范式（`_OPTIMIZER_REPORT_PATH` / `get_report` / `generate_report`）
- `rewrite_evaluator` A/B 对比 + `REWRITE_EVAL_MARGIN` 防抖动思路
- `config_service` runtime 热读模式（ef/temperature 已用）——本方案不用，但证明模式可复用

## 4. 架构设计

### 4.1 数据流

```
admin 点"重新扫描"（POST /system/retrieval/tune）
   ▼
retrieval_tune_service.run_scan(db)  （异步：①队列 default / 后台 create_task）
   ├─ 1) baseline: mixed_search(overrides=None) 跑 golden 12 条 → recall/MRR/nDCG/无结果率
   ├─ 2) 扰动: 每参数取候选值, mixed_search(overrides={RRF_K:40}) 跑 golden
   ├─ 3) 开关: 逐个翻转 RERANK/HyDE/multi-query/small-to-big 跑 golden
   └─ 4) 对比 baseline, 提升>margin 才出建议 → 写 backend/data/tune_report.json
   ▼
GET /system/retrieval/tune/report → Admin「检索调参」tab
   （参数人工改 .env 重启，系统不动 settings）
```

### 4.2 mixed_search overrides 改造（关键，让扫描能跑不同参数）

`retrieval_service.mixed_search` 加可选 `overrides: dict | None = None`，默认 None 走 `settings`（现有 13 个 caller 零破坏）：

```python
def _ov(ov: dict | None, key: str, default):
    return ov.get(key, default) if ov else default

async def mixed_search(db, query, topk=10, ..., overrides: dict | None = None) -> list[dict]:
    rrf_k      = _ov(overrides, "RRF_K", settings.RRF_K)
    dense_w    = _ov(overrides, "RRF_DENSE_WEIGHT", settings.RRF_DENSE_WEIGHT)
    sparse_w   = _ov(overrides, "RRF_SPARSE_WEIGHT", settings.RRF_SPARSE_WEIGHT)
    mmr_lambda = _ov(overrides, "MMR_LAMBDA", settings.MMR_LAMBDA)
    rerank_on  = _ov(overrides, "RERANK_ENABLE", settings.RERANK_ENABLE)
    # ... 内部所有 settings.XX 同理替换；topk 可被 overrides["TOPK"] 覆盖
```

扫描传 `overrides={"RRF_K": 40}`，生产不传 → 行为不变。

### 4.3 retrieval_eval_service（服务化，直接调 mixed_search）

```python
async def evaluate_over_golden(db, overrides: dict | None = None, topk: int = 5) -> dict:
    golden = _load_golden()
    recalls, mrrs, ndcgs, n_empty = [], [], [], 0
    for item in golden:
        ctx = await retrieval_service.mixed_search(db, item["query"], topk, overrides=overrides)
        if not ctx: n_empty += 1; continue
        got = [c["docName"] for c in ctx]
        recalls.append(_recall_at_k(item["expect"], got))
        mrrs.append(_mrr(item["expect"], got))
        if item.get("relevant_docs"): ndcgs.append(_ndcg(item["relevant_docs"], got))
    return {"recall": _mean(recalls), "mrr": _mean(mrrs), "ndcg": _mean(ndcgs),
            "noResultRate": n_empty / len(golden), "sampleSize": len(golden)}
```

### 4.4 扫描引擎 + 参数空间

| 参数 | 类型 | 候选值（除当前值） |
|---|---|---|
| RRF_K | 连续 | 40, 80 |
| RRF_DENSE_WEIGHT / SPARSE_WEIGHT | 连续 | (0.7,1.3) / (1.3,0.7) |
| MMR_LAMBDA | 连续 | 0.3, 0.7 |
| topk | 离散 | 3, 8 |
| CRAG_HIGH / CRAG_LOW | 连续 | ±0.1 |
| RERANK / HyDE / multi-query / small-to-big | 开关 | 翻转 |

## 5. 建议规则（四道护栏，防噪声 + 防过拟合）

| 门槛 | 默认 | 作用 |
|---|---|---|
| 提升 ≥ `TUNE_MIN_IMPROVE` | 0.02 | 防噪声 |
| 有效样本 ≥ `TUNE_MIN_SAMPLE` | 10 | 防小样本误判 |
| 同参数只出最优候选 | — | 取候选里提升最大那一条 |
| 多指标同向 | — | recall 升但 MRR 大降 → 降 confidence |

建议对象：`{param, current, suggested, metric, delta, confidence, reason}`，confidence = 高(≥5% & 多指标同向)/中(2-5%)/低(单指标升另一降)。

## 6. 报告 + 端点 + Admin + metrics + 配置

**报告**（`backend/data/tune_report.json`，复用 feedback_optimizer 范式）：`{baseline, suggestions, scanMatrix, switches, runAt, duration, evalCount}`。

**端点**（RBAC：复用 `SYSTEM_CONFIG` 给 admin）：
| 端点 | 作用 | 限流 |
|---|---|---|
| `POST /system/retrieval/tune` | 触发扫描（异步：①队列 / create_task） | `1/minute` |
| `GET /system/retrieval/tune/report` | 读报告缓存 | 无 |

**Admin「检索调参」tab**：baseline 卡片 + 建议表（**「复制 .env 行」按钮**）+ 扫描矩阵 echarts 折线 + 开关 A/B 明细 + 重新扫描按钮。

**metrics**：`grid_retrieval_tune_total`（扫描次数）/ `grid_retrieval_baseline{metric}`（recall/MRR/nDCG 进 Gauge → Grafana 检索质量趋势）。

**配置**：
```
TUNE_ENABLE: bool = True
TUNE_MIN_IMPROVE: float = 0.02
TUNE_MIN_SAMPLE: int = 10
TUNE_SCAN_TOPK: int = 5
```

## 7. 测试

- **纯函数**：`_recall_at_k`/`_mrr`/`_ndcg`（已知 golden→已知值）；建议规则（mock mixed_search，断言 >margin 出建议、<margin 不出、多指标降 confidence）。
- **overrides**：mixed_search 传 overrides 用 override 值、不传走 settings（保护 13 caller）。
- **回归**：现有 13 个 mixed_search caller 全绿。
- **集成**：起服务 POST `/system/retrieval/tune` → 报告生成。

## 8. 风险

| 风险 | 规避 |
|---|---|
| golden 小样本过拟合 | `TUNE_MIN_SAMPLE` + 只建议不自动应用 |
| 扫描慢（27×12×检索+rerank） | 异步 + `1/minute` 限流 + 报告缓存 |
| mixed_search 改动影响 13 caller | overrides 默认 None + 单测默认路径 |
| 百炼欠费致 embed/rerank 失败 | 降级（degraded）+ 报告标注「评测不完整」 |

## 9. 验收标准

- [ ] `mixed_search(overrides=None)` 行为与改造前完全一致，13 caller 回归全绿。
- [ ] `POST /system/retrieval/tune` 跑完产出 `tune_report.json`，含 baseline + suggestions + scanMatrix + switches。
- [ ] 建议每条满足四道护栏（提升≥2% / 样本≥10 / 最优候选 / 多指标同向）。
- [ ] Admin tab 展示 baseline 卡片 + 建议表 + 「复制 .env 行」可复制 `RRF_K=40`。
- [ ] 扫描矩阵 echarts 折线能看出每参数 recall 拐点。
- [ ] `grid_retrieval_baseline{metric}` 进 Grafana，检索质量趋势可观测。
- [ ] 百炼欠费时扫描降级，报告标注「评测不完整」，不 500。
