# 缓存方法论

> 基于 `backend/app/clients/redis_client.py` / `services/cache_persist.py` / `services/cache_warmup.py` / `services/qa_service.py` / `services/feedback_optimizer_service.py` / `core/obs.py` 源码整理（codegraph 核实）。

## 一、三级缓存是什么

问答结果按"快→慢、易失→持久"分三层，命中即返回，miss 才走下一层：

```
问 query → [黑名单?] → Redis L1(热点) → MySQL L2(持久) → Semantic L1.5(相似) → LLM L3(生成)
                                                              ↑命中回填 Redis
```

- **Redis L1**：内存热点缓存，毫秒级。`qa:{model}:{nq}` key，TTL 3 天（分层时按 docType 动态）。
- **MySQL L2**：`qa_cache` 表持久缓存，Redis 过期/evict 时兜底。`query_hash`(MD5) 精确匹配。
- **Semantic L1.5**：embedding 相似度模糊命中（默认关 `SEMANTIC_CACHE_ENABLE`）。
- **LLM L3**：检索 + 生成（最慢，最贵）。

## 二、key 与 TTL

- **key**：`_cache_key(model_type, nq) = f"qa:{model_type or 'default'}:{nq}"`（`qa_service.py:19`），nq 是 `term_service.normalize(query)` 归一化后。
- **TTL**：`QA_CACHE_TTL=259200`（3 天）。分层 TTL 开启时（`CACHE_TIERED_TTL_ENABLE`）按 docType：手册 7d / 案例 3d / 实时 5min（`QaCache.ttl_for_query`）。
- **Redis 淘汰**：`maxmemory-policy=allkeys-lru`，maxmemory 300MB（满则 LRU 淘汰最久未用）。

## 三、写 Redis 的 4 个触发点（`cache_set_json` 系）

| # | 触发 | 位置 | TTL |
|---|---|---|---|
| 1 | **实时写**（LLM 生成后，Write-Through 先 MySQL 后 Redis）| `qa_service.py` answer/stream 写缓存块 | QA_CACHE_TTL |
| 2 | **回填写**（MySQL 命中时异步回填 Redis，`asyncio.ensure_future`）| `cache_persist.py:59` `cache_get_mysql` | QA_CACHE_TTL |
| 3 | **启动预热·热点**（MySQL hit_count Top-50 回写）| `cache_warmup.py:53` `warmup_hot_queries` | QA_CACHE_TTL |
| 4 | **启动预热·golden**（golden_qa.json 预载）| `cache_warmup.py:95` `warmup_from_file` | QA_CACHE_TTL |

> `cache_set_json_persistent`（无 TTL）只用于配置持久化（`config:milvus`/`config:model`），非问答。

## 四、写缓存的条件（关键规则）

```python
(is_single or conversation_id) and confidence == "high" and not 黑名单
```

- **轮次**：单轮 或 多轮都写（多轮高置信也写，用户点推荐问题场景）
- **置信度**：只写 `confidence=="high"`；证据有限(medium)/不足(refused) **不写**——防低质/编造答案被缓存喂人
- **黑名单**：query 在 Redis set `qa:cache:blacklist` 时不写（反馈驱动）

## 五、读缓存的条件

```python
# 单轮：读
if is_single and not 黑名单: 读 Redis→MySQL→Semantic
# 多轮：仅完整 query(search_q==nq) 才读
if conversation_id and search_q == nq and not 黑名单: 读 Redis
```

> 多轮指代问题（"它呢"，`search_q != nq`）**不读**——答案依赖上下文，跨对话命中会脏。多轮指代会写（高置信）但读时过滤，靠 LRU 兜底淘汰。

## 六、黑名单（反馈驱动，`feedback_optimizer_service.py`）

- **来源**：dislike 累计≥`OPTIMIZER_BLACKLIST_THRESHOLD`(2) 自动入库；管理员手动 `POST /system/optimizer/blacklist`
- **存储**：Redis set `qa:cache:blacklist`（归一化 query）
- **拦截**：`_is_blacklisted(nq)` 读 `SISMEMBER`，命中则跳过缓存读+写（强制重走 LLM）
- **三层一次性封堵**：读条件包整个缓存块（Redis+MySQL+Semantic 全跳）

## 七、失效（`invalidate_cache_on_dislike` + 文档更新）

- **dislike 失效**：用户点踩 → 失效该 query 全部缓存。**精确匹配** `query_normalized == nq`（对齐 MD5 hash 机制）+ **删 Redis L1**（SCAN `qa:*:{nq}`）。旧版前缀匹配 `query.like(prefix%)` 已废弃（误删/漏删）。
- **文档更新失效**：`cache_invalidate_for_doc_async(doc_id)` 用 `JSON_SEARCH(retrieval_sources, docId)` 软删关联缓存。
- **bg task 用独立 `AsyncSessionLocal`**（不共享请求 db，防 session 并发 500）。

## 八、预热（启动 lifespan，`main.py:96-108`）

```
init_db → bge 预热 → metrics 预注册 → config 载入 → 后台清理/指标 task
→ warmup_hot_queries(Top-50)   ← MySQL hit_count 高的回写 Redis
→ warmup_from_file(golden)     ← golden_qa.json 预载
```

日志打印「热点预热 N 条 / golden 预热 M 条」。

## 九、降级（`core/obs.py`）

所有缓存操作 `try/except + degraded(tag, e)`：
- Redis 挂 → `degraded("qa_cache_get/set")`，跳过缓存走 LLM（不阻塞业务）
- MySQL 写失败 → `degraded("qa_cache_mysql_set")` + `CACHE_MYSQL_FAIL.inc()`
- 全部降级进 `metrics.DEGRADED.labels(tag)`，Grafana 可见（盲降级不再被吞）

## 十、踩坑

1. **bg task session**：失效/记事件等后台任务必须用独立 `AsyncSessionLocal`，共享请求 db 会触发 `IllegalStateChangeError` 500（请求 close session 时 bg task 还在用）。
2. **前缀匹配误删**：旧 `query.like(prefix%)` 和 MD5 hash 精确匹配机制不对齐，已改 `query_normalized == nq`。
3. **认证响应**：项目 HTTP 恒 200，业务码在 body（测试断言踩坑）。
4. **改代码必须 rebuild**：源码 bake 进镜像无 bind mount，改 backend 要 `docker compose up -d --build backend`。
