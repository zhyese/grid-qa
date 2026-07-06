---
name: cache-debug
description: 排查问答缓存问题（不命中/脏缓存/黑名单误拦/置信度过滤/多轮不缓存）时使用。覆盖三级缓存(Redis/MySQL/Semantic)+黑名单+置信度规则。
---

# 问答缓存排查

## 何时用
- "同一个问题怎么不命中缓存？"
- "缓存返回了错误/旧答案（脏缓存）"
- "这个问题为什么不缓存？"
- "缓存命中率低"

## 排查清单（按顺序）

### 1. 该 query 真的没命中吗？看命中层
问一次，看响应 `cacheLayer` 字段：
```bash
# 流式 done 事件或 answer 响应里
cached=True layer=redis   # 命中 L1
cached=True layer=mysql   # 命中 L2
cached=True layer=semantic_high  # 命中 L1.5
cached=False layer=llm    # 全 miss，走 LLM
```

### 2. key 对不对？查 Redis
```bash
# nq = term_service.normalize(query)，缓存 key = qa:{model}:{nq}
docker exec grid-redis redis-cli --scan --pattern "qa:*:{归一化query}"
docker exec grid-redis redis-cli GET "qa:default:主变压器日常巡视检查哪些项目"
```
key 不存在 → 没写过（看步骤 4 写条件）或被 LRU 淘汰/黑名单失效。

### 3. 黑名单拦了吗？
```bash
docker exec grid-redis redis-cli SISMEMBER "qa:cache:blacklist" "归一化query"
# 返回 1 = 在黑名单（强制重走 LLM，不读不写）
```
黑名单来源：dislike≥2 自动 / 管理员手动加。移除：`SREM qa:cache:blacklist "query"`。

### 4. 为什么没写缓存？核对写条件
写 Redis 需**全部满足**（`qa_service.py` answer/stream 写缓存块）：
```
(is_single or conversation_id) and confidence == "high" and not 黑名单
```
- 多轮指代问题（`search_q != nq`）：会写但读时过滤（不脏命中）
- `confidence != "high"`（证据有限 medium / 不足 refused）：**不写**（防低质答案扩散）→ 看检索质量/CRAG 分级
- 黑名单：不写

### 5. 多轮不命中？
多轮只对**完整 query**（`search_q == nq`，standalone 没改写）读缓存。指代问题（"它呢"）不读（跨对话脏）。确认是不是指代问题。

### 6. MySQL L2 有吗？
```bash
docker exec grid-mysql mysql -uroot -p<pwd> rag -e \
  "SELECT cache_key,hit_count,expires_at,is_deleted FROM qa_cache WHERE query_normalized='<nq>'"
```
`is_deleted=1` = 软删（dislike/文档更新触发）；`expires_at < NOW()` = 过期。

### 7. 降级了吗？
```bash
docker logs grid-backend 2>&1 | grep "降级:.*cache"
# 或 Grafana grid_degraded_total{tag=~".*cache.*"}
```
Redis 挂/MySQL 写失败会降级走 LLM（不阻塞业务但命中率掉）。

## 常见根因速查

| 症状 | 大概率根因 | 验证 |
|---|---|---|
| 全 miss 走 LLM | key 不存在 / 黑名单 / confidence≠high | 步骤 2/3/4 |
| 脏答案 | 多轮指代跨对话（不该命中却命中？检查 search_q）| 步骤 5 |
| 命中率低 | LRU 淘汰（内存不够）/ 写条件过严 / 预热没跑 | `redis-cli INFO memory` + 步骤 4 + 启动日志 |
| 改完代码没生效 | 源码 bake 进镜像，没 rebuild | `docker compose up -d --build backend` |

## 关键代码位置（codegraph 核实）
- 写条件/读条件：`backend/app/services/qa_service.py`（answer + stream_answer 缓存块）
- Redis 读写：`backend/app/clients/redis_client.py`（cache_get/set_json）
- MySQL L2：`backend/app/services/cache_persist.py`（cache_get/set_mysql + 回填）
- 黑名单：`backend/app/services/feedback_optimizer_service.py`（is_query_blacklisted）
- 预热：`backend/app/services/cache_warmup.py`（warmup_hot_queries/from_file）
- 降级：`backend/app/core/obs.py`（degraded）

> 用 codegraph 快速定位：`codegraph explore "_cache_key cache_set_json cache_get_mysql is_query_blacklisted warmup"`
