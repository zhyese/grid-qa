---
name: degradation-check
description: 排查"功能静默失效"（rerank 没生效/图谱没融合/缓存没命中）时使用。项目所有降级走 DEGRADED 指标+日志，用它定位。
---

# 降级 / 可观测排查

## 核心机制
项目所有 `except` 走 `degraded(tag, e)`（`backend/app/core/obs.py`）：记 `metrics.DEGRADED.labels(tag).inc()` + `[降级:tag]` warning 日志。**没有盲 `except: pass`**——失败必须可见。

## 何时用
- "rerank 好像没生效"（百炼欠费？）
- "知识图谱没融合进答案"（Neo4j 没启？）
- "缓存命中率突然掉"（Redis 挂？）
- "功能时好时坏"（某路间歇降级）

## 排查步骤

### 1. 看降级计数（哪个 tag 在涨）
```bash
# Prometheus 查询（Grafana 或 curl /metrics）
curl -s http://localhost:8001/metrics | grep "grid_degraded_total" | sort -t' ' -k2 -rn
```
或 Grafana 降级面板（`sum by (tag) (increase(grid_degraded_total[5m]))`）。

### 2. 看降级日志（具体原因）
```bash
docker logs grid-backend 2>&1 | grep "降级:" | tail -30
# 例：[降级:rerank] ArrearsError: 账户欠费
#     [降级:kg_neo4j] ConnectionRefusedError: Neo4j 未启动
#     [降级:qa_cache_get] ConnectionError: Redis 挂
```

### 3. 常见降级 tag 速查
| tag | 含义 | 根因 |
|---|---|---|
| `rerank` | rerank 调用失败 | 百炼欠费/key 失效/网络；pool 保持原序 |
| `kg_neo4j` / `kg_graph_context` | 知识图谱融合失败 | Neo4j 没启；graph=[] |
| `qa_cache_get/set` | 缓存读写失败 | Redis 挂；跳缓存走 LLM |
| `semantic_cache_*` | 语义缓存失败 | Redis/embedding；跳过 |
| `crag_v2` / `crag_rewrite` | CRAG 分级/改写失败 | LLM 调用；回退 v1 |
| `embed_*` | embedding 失败 | provider 挂/网络 |
| `hyde_dispatch` / `multi_query_dispatch` | HyDE/多查询失败 | LLM；跳过走原 query |
| `*_mysql_*` | MySQL 读写失败 | DB 连接/锁 |
| `feedback_judge*` | dislike judge 失败 | LLM；judge 字段不回填 |
| `rewrite_cache_*` / `rewrite_eval` | 改写缓存/评估失败 | Redis/embedding |

### 4. 验证组件健康
```bash
curl -s http://localhost:8001/health | python -m json.tool
# checks: mysql/minio/milvus/redis → ok?
# providers: llm/embedding keyConfigured?
curl -s http://localhost:8001/api/system/health/providers -H "Authorization: Bearer $TOK"
# provider 主动 ping（抓欠费/key失效）
```

### 5. 指标未出（面板 No data）
事件驱动指标（FEEDBACK/CRAG/ROUTING/REWRITE）**事件发生前 /metrics 不含该序列** → Grafana "No data"。检查 `init_metric_series()`（`metrics.py`）是否预注册了该 label。或 Grafana 面板用 `or vector(0)` 兜底。

## 关键代码
- 降级入口：`backend/app/core/obs.py`（degraded）
- 指标定义 + 预注册：`backend/app/core/metrics.py`（DEGRADED + init_metric_series）
- 健康探活：`backend/app/routers/system.py`（/health, /system/health/providers）+ 后台 `_refresh_component_health_loop`

> codegraph 定位：`codegraph explore "degraded DEGRADED init_metric_series COMPONENT_HEALTH check_llm_health"`
