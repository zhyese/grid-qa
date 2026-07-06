# Query 改写升级设计（方案 A · 轻量全套）

> 2026-07-06 · brainstorming 产出 · 待 review

## 1. 背景

项目 query 改写「全家桶」骨架齐全：`rewrite_query`（口语→规范）/ `standalone_query`（多轮指代消解）/ `multi_query.decompose`（多查询分解）/ `hyde.generate_hypothetical`（假设文档检索），外加 CRAG incorrect 时 force 改写、`term_service.normalize`（词典归一）。`mixed_search` 编排（rewrite→multi_query→dense+HyDE+BM25→RRF→rerank→MMR→small-to-big→RAPTOR）是业界主流全栈。

**编排层不 toy，改写策略层 toy**，四个通病：
1. **盲用无评估**——4 个改写函数改完直接用，不判断「改写后检索是否更优」
2. **全开/全关无 adaptive**——业界最佳实践是「低置信才 fallback HyDE/multi-query」省延迟，项目开关式一刀切
3. **无改写缓存**——相同 query 每次都调 LLM（`term_service` 有 `lru_cache`，LLM 改写没有）
4. **rewrite_query prompt 最 toy**——单段笼统 prompt，无 few-shot、无电网领域示例、无策略分类

## 2. 目标

用**已有基础设施**（RRF 分数、Redis、`query_classifier`）补齐评估闭环 + 缓存 + adaptive 调度 + prompt 工程，把改写从 toy 升级到生产可用，延迟可控（评估加的 1 次轻量检索被 adaptive 跳过的 simple query 抵消）。

## 3. 非目标

- 不引入 LLM judge 评估改写质量（留后续迭代）
- 不做离线 golden 集回归（后续）
- 不做 DMQR-RAG 4 策略完整版（multi_query 仅加多样性提示）
- 不改 `mixed_search` 的 RRF/rerank/MMR/small-to-big/RAPTOR 编排

## 4. 架构——4 个新组件，全接已有设施

### 4.1 RewriteStrategyClassifier（新，纯规则）
- **职责**：判 query 类型，选对应 prompt + few-shot
- **类型**：口语化 / 缩写 / 术语缺失 / 正常
- **规则**（无 LLM，纯词表 + 长度）：
  - 口语化：长度 < 8 或含口语词集（咋/咋办/咋整/啥/啥叫/嘛/啥样）
  - 缩写：含电网缩写集（CT/PT/SF6/GIS/VT/AVR/RTU/SCADA…）
  - 术语缺失：`term_service` 反查，query 含非标准别名（→ 该走 normalize，不必 LLM 改写）
  - 正常：以上都不满足 → **跳过改写**
- **接口**：`classify(query) -> {"type": str, "skip": bool, "hint": str}`
- **依赖**：`term_service`
- **兼任 adaptive 总开关**：`skip=True`（正常 query）即「不改写」；`REWRITE_ADAPTIVE_ENABLE=False` 时 skip 不生效（所有 query 进改写，等价旧行为）。这样 adaptive 不另依赖 `query_classifier`——改写难度与 routing 难度语义不同，自包含规则更内聚。

### 4.2 RewriteCache（新，Redis）
- **职责**：缓存 LLM 改写结果，相同 query 不重复调
- **key**：`rewrite:{strategy}:{md5(query)}`（strategy ∈ rewrite/multi/hyde）
- **value**：JSON `{result, improved, ts}`
- **TTL**：`REWRITE_CACHE_TTL`（默认 7 天）
- **接口**：`get(strategy, query) -> dict | None` / `set(strategy, query, value)`
- **依赖**：`redis_client`
- **失效**：文档更新走既有 `cache_invalidate_for_doc_async`（不额外处理，改写映射相对稳定）

### 4.3 RewriteEvaluator（新）
- **职责**：判断改写是否比原 query 召回更优
- **方法**：对 original / rewritten 各跑一次**轻量检索**（仅 dense_cloud，cand=10，跳 rerank/MMR），算 top-K（K=5）的分数和，`rewritten_score > original_score * (1 + margin)` 才算更优
- **margin**：`REWRITE_EVAL_MARGIN`（默认 0.05，防分数抖动误判）
- **接口**：`evaluate(original, rewritten, db, model_type) -> {"improved": bool, "orig_score": float, "new_score": float}`
- **依赖**：`embedding_service`、`milvus_client`、`rrf`
- **只管 `rewrite_query`**；multi_query 是扩召回（不评估单个）、hyde 是换 dense_q（不评估）

## 5. 数据流（`mixed_search` step 0 改造）

```
query
 → strategy = Classifier.classify(query)
 → if REWRITE_ADAPTIVE_ENABLE and strategy.skip → q = query（跳过改写，省 LLM + 评估延迟）
   else → rewrite_with_cache_and_eval(query, strategy)
        1. (strategy 已由上一步得到)
        2. if strategy.skip → return query
        3. cached = RewriteCache.get(strategy, query); if cached → return (cached.improved ? cached.result : query)
        4. rewritten = rewrite_query(query, strategy)   # 带 few-shot
        5. if REWRITE_EVAL_ENABLE → improved = Evaluator.evaluate(query, rewritten).improved
        6. result = improved ? rewritten : query
        7. RewriteCache.set(strategy, query, {result, improved})
        8. return result
 → multi_query(如开,同样 RewriteCache strategy=multi) → dense(+HyDE,同样缓存 strategy=hyde) → BM25 → RRF → rerank → MMR → ...
```

## 6. 错误处理（全走 `degraded`，记 `metrics.DEGRADED`）

- Classifier 异常 → 默认 normal（不跳过，走原 rewrite）
- Cache get/set 异常 → 跳缓存直调 LLM
- Evaluator 检索异常 → `improved=False`（回退原 query）
- Adaptive 异常 → 默认走全套（安全兜底）

新增指标：`rewrite_improved_total` / `rewrite_cache_hit_total` / `rewrite_eval_rejected_total`（改写被评估否决次数）。

## 7. 测试（TDD）

- `test_rewrite_strategy`：口语/缩写/术语/正常 4 类各一例
- `test_rewrite_evaluator`：mock 检索分数，验证 improved 判定 + margin + 平局回退原 query
- `test_rewrite_cache`：命中/失效/TTL/异常降级
- `test_adaptive`：simple query 跳过改写
- `test_rewrite_with_eval`：集成（改写更优用改写，否则用原 + 写缓存）
- `test_mixed_search_e2e`：golden query 端到端，断言不退化（recall 不降）

## 8. 配置（config.py 新增）

```
REWRITE_CACHE_TTL: int = 604800        # 改写缓存 TTL（7 天）
REWRITE_EVAL_ENABLE: bool = True       # 评估闭环开关（可关降延迟）
REWRITE_ADAPTIVE_ENABLE: bool = True   # Classifier 判正常 query 时跳过改写（False=全部改写，等价旧行为）
REWRITE_EVAL_MARGIN: float = 0.05      # 评估更优阈值（防抖动）
REWRITE_EVAL_CAND: int = 10            # 评估检索候选数
REWRITE_EVAL_TOPK: int = 5             # 评估取 top-K 算分数和
```

## 9. 文件清单

**改**：
- `backend/app/services/query_rewrite.py`（接入 Classifier + Cache + Evaluator + few-shot；保留 force 路径绕过 adaptive/评估）
- `backend/app/services/retrieval_service.py`（`mixed_search` step 0 接 Classifier + Cache + Evaluator；multi_query/hyde 加 Cache）
- `backend/app/config.py`（6 字段）
- `backend/app/core/metrics.py`（3 计数器 + init_metric_series 预注册）

**新**：
- `backend/app/services/rewrite_strategy.py`（Classifier）
- `backend/app/services/rewrite_cache.py`（Cache）
- `backend/app/services/rewrite_evaluator.py`（Evaluator）
- `backend/app/data/rewrite_fewshot.json`（电网 few-shot 示例库，按类型组织）
- `tests/test_rewrite_strategy.py` / `test_rewrite_evaluator.py` / `test_rewrite_cache.py`

## 10. 风险与权衡

- **评估加 1 次检索（~50ms）**：adaptive 跳过 simple query 反而省延迟，整体延迟持平或下降
- **评估用 dense_cloud 单路（非全链路）**：信号粗但够用，避免双倍 rerank 成本
- **margin 0.05**：防分数抖动误判；可配置
- **缓存 7 天**：知识库变更时改写结果可能过时——但 query→改写 映射相对稳定，且文档更新失效缓存已有机制

## 11. 验收标准

1. simple query 跳过改写（adaptive 生效，metrics 可见）
2. 改写更优时用改写、否则用原（评估闭环，`rewrite_eval_rejected_total` 可观测）
3. 相同 query 二次不调 LLM（缓存命中，`rewrite_cache_hit_total` 上升）
4. 口语 query 改写带 few-shot 效果 ≥ 旧 prompt（golden 对比不退化）
5. 全链路不退化（`test_mixed_search_e2e` 通过）
6. CRAG force 改写仍工作（不受 adaptive/评估影响）
