# 问答全链路优化 — 设计文档

- **日期**：2026-07-19
- **状态**：Draft（分批实施中）
- **背景**：可核验引用体系（commit c94bd52）完成后，对检索算法/数据链路/问答链路做全面优化调研，落地 16 项。
- **硬约束**：全 opt-in（新开关默认关，关时=现状）；前端零改动；每批独立可测 commit；显式 git add；main 分支。

---

## 总览：16 项分 4 批

| 批 | 主题 | 项 | 风险 | 依赖 |
|---|---|---|---|---|
| Batch 1 | 检索算法 routing-aware 调参 | A2/A3/A5/B6 | 低（调参） | 无 |
| Batch 2 | chunk 向量复用 | A1/A4 | 中（retrieval+verifier） | 无 |
| Batch 3 | 数据链路基础设施 | B1/B2/B3/B4/B5 | 低（索引/缓存/usage） | 无 |
| Batch 4 | 问答链路重构 | C1/C2/C3/C4/C5 | 中-高（异步/流式/联动） | C3 依赖 C1 |

---

## Batch 1 · 检索算法 routing-aware 调参

核心：让 mixed_search 根据 query_classifier 的 RoutingDecision 动态调参（而非固定 RRF 等权/MMR λ/ef/rerank 范围）。

### A2 · RRF 动态加权
- **现状**：`retrieval_service.mixed_search` `rrf_fuse([all_dense, all_sparse], weights=[RRF_DENSE_WEIGHT, RRF_SPARSE_WEIGHT])`（默认 1.0/1.0 等权）
- **改**：按 `routing_decision.route` 调权——dense 路径 dense×1.3、sparse 路径 sparse×1.3、hybrid 等权。新增 config `RRF_ROUTE_AWARE_ENABLE`（默认关）。
- **文件**：`backend/app/services/retrieval_service.py:282`（hybrid 分支 rrf_fuse 调用处）
- **测试**：mock routing_decision，断言不同 route 下 weights 不同。

### A3 · MMR λ 动态
- **现状**：`mmr.mmr(pool, topk, MMR_LAMBDA)` 固定 λ=0.5
- **改**：按 query_classifier 类型动态——fault→0.7（精度）、mixed→0.5、natural→0.4（多样）。复用 routing_decision.features.query_type。
- **文件**：`retrieval_service.mixed_search` MMR 调用处（:337）
- **测试**：不同 query_type 断言 λ。

### A5 · rerank 早剪枝
- **现状**：`ranked = await rerank(..., top_n=min(topk*2, len(pool)))`（rerank topk*2 全量）
- **改**：RRF 后先取 `topk*1.2` 再 rerank（省 rerank 额度，精度损失可忽略）。
- **文件**：`retrieval_service.mixed_search` rerank 块（:292-296）
- **测试**：断言 rerank 入参 pool 长度 ≤ topk*1.2。

### B6 · Milvus ef 动态
- **现状**：`_ef = max(config_service.rt_ef(), cand)`（固定上限）
- **改**：sparse 路径 ef↓（精确匹配不需高 ef）、dense 故障路径 ef↑（召回）。
- **文件**：`retrieval_service._dense_and_sparse` / dense 分支
- **测试**：断言不同 route 下 ef。

---

## Batch 2 · chunk 向量复用（核心降本）

### A1+A4 · 校验2 / auto_cite 复用 Milvus chunk 向量
- **现状**：`citation_verifier.verify` 校验2 + `citation.auto_cite` 都对 chunk 调 `embed_texts(chunk_texts)` 重新 embed。但 Milvus search 时 chunk 向量**已在索引/payload**，retrieval 已算过。
- **改**：
  1. `milvus_client.search` output_fields 不含 vec（向量不在 payload，是索引向量）。→ retrieval dense search 时，hit 已有 score（cosine），但**完整 vec 不在 hit**。
  2. 方案：retrieval `_to_item` 不带 vec（太大），但**新增 chunk_vec 缓存**（`chunk_embed:{chunk_id}` Redis，TTL 长，chunk 内容不变则 vec 不变）。校验2/auto_cite 先查缓存，miss 才 embed。
  3. 或更简：校验2 直接用 Milvus 再查一次 sentence vs collection（复用 Milvus 算 cosine，不 embed 到后端）。但跨服务。
- **采纳方案**：chunk_vec Redis 缓存（`embed_texts` 包装：先查 chunk_id 缓存，miss embed+存）。chunk 内容稳定→缓存命中率高。
- **文件**：`embedding_service.embed_texts`（加可选 chunk_id 参数走缓存）+ `citation_verifier`/`auto_cite` 传 chunk_id
- **测试**：同 chunk 二次 embed 命中缓存（embed 调用计数不增）。

---

## Batch 3 · 数据链路基础设施

### B1 · chunks 复合索引（Task 6 Minor）
- **现状**：`Chunk` 仅 `ix_chunks_doc_parent(doc_id, parent_idx)`；citation_index 回填 + small-to-big 按 `(doc_id, chunk_idx)` 查→全表扫
- **改**：加 `Index("ix_chunks_doc_idx", "doc_id", "chunk_idx")` + Alembic 迁移 + init_db _COLUMN_MIGRATIONS（索引无列概念，用 CREATE INDEX 幂等）
- **文件**：`backend/app/models/chunk.py` + 新 Alembic + init_db
- **测试**：EXPLAIN 确认走索引（或回归测试不破坏）

### B2 · 缓存命中滑动续期
- **现状**：Redis L1 命中不刷新 TTL，热 query 可能 evict
- **改**：`cache_get_json` 命中时 `EXPIRE key QA_CACHE_TTL`（滑动续期），config `CACHE_SLIDE_TTL_ENABLE`（默认关）
- **文件**：`backend/app/clients/redis_client.cache_get_json` 或 qa_service 命中后
- **测试**：命中后 TTL 刷新。

### B3 · embedding 缓存 TTL 分级
- **现状**：`embed_query` 固定 `ex=3600`
- **改**：高频 query 命中续期（同 B2），低频自然过期。复用 B2 思路。
- **文件**：`embedding_service.embed_query`
- **测试**：命中续期。

### B4 · 成本追踪真实 usage
- **现状**：`record_token_usage(... len(str(messages))//2, len(ans)//2)` 估算
- **改**：openai SDK `chat` 返回 `response.usage`（prompt_tokens/completion_tokens），透传。provider chat 返回值改（str → 带 usage）或单独获取。
- **文件**：`providers/llm/*.py` chat 返回 usage + `qa_service` 传真实 token + `cost_tracker_service`
- **测试**：mock provider 返回 usage，断言记录真值。

### B5 · GraphRAG 分词缓存
- **现状**：`kg_service.graph_context` 每次查询 jieba.cut
- **改**：同 query 分词结果 Redis 缓存（`jieba:{query}`）
- **文件**：`kg_service.graph_context`
- **测试**：同 query 二次不重分词。

---

## Batch 4 · 问答链路重构

### C1 · NLI 异步后置（解锁 NLI 不拖首字）
- **现状**：开 `CITATION_NLI_ENABLE` 后 `_verify_claims` 同步阻塞首答（增延迟，所以你注释着）
- **改**：校验1（格式）+校验2（cosine）同步（快，影响输出 drop/rewrite）；校验3 NLI **异步后置**（done 后后台跑，结果写 citationVerified.nli 异步覆盖，前端拉取——复用 faithfulness 异步模式）。
- **文件**：`citation_verifier.verify`（nli_enable 拆 sync/async）+ `qa_service`（NLI 异步 task）+ 新端点或复用 /qa/faithfulness
- **测试**：同步路径不调 NLI；异步 task 跑 NLI 更新结果。

### C2 · CRAG/citation rewrite 合并
- **现状**：CRAG incorrect→rewrite 一次 LLM；citation rewrite_needed→**又一次**（最坏两次 rewrite）
- **改**：citation rewrite_needed 时复用 CRAG 已 rewrite 的 contexts（若 CRAG 已 rewrite 且 citation 仍 needed→直接 refused，不二次 rewrite）；或合并到 CRAG 分级一次。
- **文件**：`qa_service.answer` rewrite 联动块（:477-511）
- **测试**：CRAG rewritten 后 citation 不再二次 rewrite。

### C3 · stream_answer 接校验（依赖 C1）
- **现状**：`answer`（非流式）接了校验；`stream_answer`（流式）结构化+校验未接
- **改**：流式仍纯文本首字（不阻塞）；done 后异步校验（C1 的 NLI 异步 + 校验1+2 同步在 done 前快速跑），结果随 done 或单独 SSE 事件下发 citationVerified。
- **文件**：`qa_service.stream_answer`（:733+）
- **测试**：流式 done 含 citationVerified（或异步事件）。

### C4 · 多轮缓存扩面
- **现状**：多轮 `search_q==nq` 才查 Redis（指代消解后重算）
- **改**：standalone query（search_q）作 key 缓存，带 conversation 版本/历史摘要 hash
- **文件**：`qa_service.answer` 多轮缓存块
- **测试**：同 standalone 多轮命中。

### C5 · 知识治理 fail-open 兜底
- **现状**：生产 tenant 治理异常→`pool=[]`（fail-closed 全拒答）
- **改**：fail-open 兜底（治理存储异常时降级放行 + DEGRADED 告警，人工复核），config `KNOWLEDGE_GOVERNANCE_FAIL_OPEN`（默认关，生产可开）
- **文件**：`retrieval_service.mixed_search` 治理块（:375-392）
- **测试**：治理异常时 fail-open 放行 + 告警。

---

## 落地顺序

1. **Batch 3**（B1/B2/B3/B4/B5）——独立基础设施，最低风险，先做
2. **Batch 1**（A2/A3/A5/B6）——检索调参，routing-aware
3. **Batch 2**（A1/A4）——chunk vec 复用，核心降本
4. **Batch 4**（C1→C2→C3→C4→C5）——问答链路，C3 依赖 C1

每 batch：失败测试 → 实现 → 回归 → 显式 git add + commit。全 opt-in 开关默认关。

---

## 不做（YAGNI）

- query_classifier 改可学习模型（规则决策树当前够用）
- 独立 NLI 模型微调（用 LLM judge 同源）
- 重写 RRF/MMR 算法本体（只加 routing-aware 权重/λ）
