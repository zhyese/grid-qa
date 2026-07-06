---
name: retrieval-trace
description: 排查检索召回差/路由偏差/改写没效果/某文档召不出问题时使用。用 debug_search 透出每步中间结果定位。
---

# 检索链路追踪

## 何时用
- "这个问题的答案召不到/召得不准"
- "明明文档里有，为什么检索不到"
- "检索结果都是同一篇文档（不够多样）"
- "路由分错了（该走 dense 却走了 sparse）"
- "query 改写有没有生效"

## 第一抓手：debug_search（透出每步）

`POST /api/retrieval/debug`（admin），与生产 mixed_search 同源但透出 trace：

```bash
TOK=$(curl -s -X POST http://localhost:8001/api/system/login -H "Content-Type: application/json" -d '{"username":"admin","password":"admin123"}' | python -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
curl -s -X POST http://localhost:8001/api/retrieval/debug -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"query":"主变压器温度异常处置","topK":5}' | python -m json.tool
```

前端：管理 → 检索调试（RetrievalDebug.vue）有界面。

## trace 怎么读（按步骤定位）

### config 快照
先看 `trace.config`：queryRewrite/hyde/multiQuery/rerank/mmr/smallToBig 哪些开了，ef/temperature 运行时值对不对。

### steps（按顺序）
| step | 看什么 | 异常信号 |
|---|---|---|
| `query_rewrite` | changed=true/false | 口语 query 没改写 → Classifier 判 normal 跳过了？或改写没更优被 Evaluator 否决 |
| `multi_query` | subQueries / totalQueries | 复杂问题没拆 → MULTI_QUERY_ENABLE 关 |
| `retrieve` | perQuery.denseHits/bm25Hits | dense=0 → embedding/索引问题；bm25=0 → BM25 没建/查不到 |
| `rrf_fuse` | fusedCount | 融合后很少 → dense+sparse 都没召到 |
| `rerank` | ok/reranked/error | ok=false error → rerank 挂（百炼欠费？）降级用原序 |
| `metadata_filter` | before→after | after 远小于 before → 过滤太严（租户/docType/设备） |
| `mmr` | applied | 结果全同一文档 → MMR 没开或 LAMBDA 太偏相关 |

### result.hits（分数归因）
每条命中的 `scores`：
- `dense`：向量分（cloud/bge）
- `bm25`：稀疏分
- `rrf`：融合分
- `rerank`：重排分（**最终排序依据**）
- `final`：最终分

> 相关文档召不出 → 看 dense/bm25 是否有该 doc；召到了但排后面 → rerank 分低（query-doc 相关性差，可能 chunk 切不好或 query 没改写）。

## 常见根因速查

| 症状 | 大概率根因 | 验证 |
|---|---|---|
| 召不到目标文档 | 该文档没向量化 / chunk 太碎 / 设备标签没打 | 管理页看文档状态；debug dense=0 |
| 召到了排后面 | rerank 分低 / query 没改写（口语）| trace rerank 分 + query_rewrite changed |
| 全是同一文档 | MMR 没开 / LAMBDA 偏相关 | config.mmr + step mmr applied |
| 改写没生效 | Classifier 判 normal 跳过 / Evaluator 否决 | step query_rewrite changed=false |
| dense 全 0 | Milvus 索引问题 / embedding provider 挂 | 降级日志 `grep 降级:.*embed\|milvus` |
| 路由分错 | query_classifier 误判 | trace.config 无 route？看 RoutingDecision.reason |

## 关键代码位置（codegraph 核实）
- 主链路：`backend/app/services/retrieval_service.py`（mixed_search:143 / debug_search:328）
- 路由：`backend/app/routing/routing_service.py`（route_query）+ `query_classifier.py`（RoutingDecision）
- 融合：`backend/app/rag/rrf.py`（rrf_fuse）
- 重排：`backend/app/services/rerank_service.py`
- 多样性：`backend/app/rag/mmr.py`
- 自纠错：`backend/app/rag/crag.py` + `qa_service._crag_correct`

> codegraph 定位：`codegraph explore "mixed_search debug_search route_query rrf_fuse _crag_correct"`
