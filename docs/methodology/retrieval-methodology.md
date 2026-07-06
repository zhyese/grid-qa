# 检索方法论

> 基于 `backend/app/services/retrieval_service.py`（mixed_search/debug_search）+ `routing/` + `rag/rrf.py` + `services/rerank_service.py` + `rag/mmr.py` + `rag/crag.py` + `qa_service._crag_correct` 源码整理（codegraph 核实）。

## 一、检索链路总览（mixed_search 7 步）

```
query
 0) query 改写（rewrite_query_v2：分类→缓存→改写→评估）
 0.5) 多查询分解（multi_query.decompose，带缓存）
 1) 按路由检索：sparse/dense/hybrid（dense 含 HyDE + 双 collection，sparse BM25）
 2) RRF 融合（dense + sparse）+ sources 归因回填
 3) 重排（gte-rerank-v2）
 4) 元数据过滤（租户 + docType + 设备）
 5) MMR 多样性选 topk
 6) small-to-big 父块召回
 7) RAPTOR 摘要层融合（可选）
→ 返回 topk 条 [{chunk, score, docId, docName, docType, sources}]
```

> CRAG 自纠错在 `qa_service._crag_correct`（检索后分级，不在 mixed_search 内）。

## 二、智能路由（`routing/routing_service.route_query`）

按查询特征选路径，省冗余检索：
- **sparse**（BM25）：短术语/标准引用（"DL/T 596"），跳过 dense embedding
- **dense**（向量）：故障口语/同义词，跳过 BM25
- **hybrid**（默认）：全链路 dense + BM25 + RRF
- **sparse_first**：BM25 优先

返回 `RoutingDecision(route, reason)`。高置信 sparse 可 `should_skip_rerank` 跳过重排（省延迟）。

> 60%+ 查询跳过冗余分支，p95 检索延迟降 30-50%。

## 三、dense + BM25（`_dense_and_sparse`）

- **dense 双 collection**：云 embedding（`MILVUS_COLLECTION`）+ 本地 bge（`MILVUS_COLLECTION_BGE`），向量空间隔离，双查并集。`asyncio.gather` 并发。
- **HyDE**（可选，`_hyde_or_cache`）：短/口语 query → LLM 生成假设文档 → 用假设文档 embedding 检索（缩小 query-doc 语义 gap）。BM25 仍用原 query（吃原词）。带 Redis 缓存。
- **BM25**（`bm25_service`）：稀疏检索，吃术语精确匹配。`ensure_built` 懒建索引。

## 四、多查询分解（`multi_query.decompose`）

复杂问题拆 N 个子问题并行检索（"对比主变和配变温度处置" → 2 个子查询），候选合并走 RRF。带 Redis 缓存（`rewrite:multi:{md5}`）。仅 hybrid/dense 路径启用，sparse 跳过。

## 五、RRF 融合（`rag/rrf.py:rrf_fuse`）

dense + sparse（或多 query 的多路结果）按 Reciprocal Rank Fusion 合并：`score = Σ 1/(k + rank)`。`_aggregate_srcs` 独立聚合每条的来源（dense_cloud/dense_bge/bm25），fuse 后回填（rrf_fuse 只留首遇 key 字段）。

## 六、重排（`rerank_service`，gte-rerank-v2）

百炼 rerank 对 RRF 融合后的 topk×2 候选重排，按 query-doc 相关性打分。失败降级（`degraded("rerank")`，pool 保持原序）。

## 七、元数据过滤（多租户 + D5）

按 tenant（租户隔离）+ docType + equipment_tags 过滤 pool。`docType` 补全（来源卡片要用，无条件查一次）。

## 八、MMR 多样性（`rag/mmr.py`）

`MMR_ENABLE` + `MMR_LAMBDA`(0.6)：相关性 vs 多样性权衡，避免 topk 全来自同一篇文档的相邻段落。

## 九、small-to-big（`_expand_parents`）

命中小块 → 聚合同组父块全文给 LLM（完整上下文，解决长规程跨块/表格被切两半）。按 `parent_idx` 分组拼接。

## 十、RAPTOR（可选）

层次化摘要检索：原文 pool + 摘要层 hits 再 RRF 融合（摘要给 0.8 权重不压过原文）。默认关。

## 十一、CRAG 自纠错（`qa_service._crag_correct`，检索后）

```
检索 contexts → 分级（CRAG v2 LLM 逐条评估 优先，回退 v1 rerank top1 分数）
  correct(高分)   → 直接用，confidence=high
  ambiguous(中等) → 用，confidence=medium
  incorrect(低分) → query 改写重检索（rewrite_query force=True）
                     仍 incorrect → refused 保守拒答（零幻觉），confidence=refused
```

把事后 LLM-judge 升级为**实时前置护栏**。指标 `CRAG_GRADE` / `CRAG_ACTION`（normal/rewritten/refused）。

## 十二、debug_search（排障 trace）

`/retrieval/debug` 接口，与 mixed_search 同源构建块但**透出每步中间结果**：config 快照 + 各步（改写/HyDE/multi-query/dense·BM25 召回数/RRF/rerank/元数据过滤/MMR）+ 最终命中的**分数归因**（dense/bm25/rrf/rerank/final）+ 多样性指标（doc_uniqueness/source_entropy/chunk_adjacency）。

> 不命中缓存、不裁剪中间态。生产 mixed_search 不受影响。**召回差时第一抓手**。

## 十三、降级 + 指标

- 全链路 `try/except + degraded(tag)`：HyDE 挂/multi_query 挂/rerank 挂/RAPTOR 挂 都降级不阻塞
- 指标：`RETRIEVAL_LATENCY`（直方图）、`CRAG_GRADE/ACTION`、各降级 `DEGRADED`

## 十四、关键开关（config）

```python
ROUTING_ENABLE=True          # 智能路由
RERANK_ENABLE=True           # 重排
MMR_ENABLE=True, MMR_LAMBDA=0.6
SMALL_TO_BIG_ENABLE=True     # 父块召回
HYDE_ENABLE=False            # HyDE（默认关，增延迟）
MULTI_QUERY_ENABLE=False     # 多查询（默认关）
CRAG_ENABLE=True             # 自纠错
CRAG_HIGH=0.6, CRAG_LOW=0.3  # 分级阈值
RAPTOR_ENABLE=False          # 摘要层（默认关）
```
