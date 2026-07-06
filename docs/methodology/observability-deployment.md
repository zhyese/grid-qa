# 可观测与部署方法论

> 基于 `backend/app/core/metrics.py` / `core/obs.py` / `main.py`(lifespan) / `config.py` + `docker-compose.yml` 源码整理（codegraph 核实）。

## 一、可观测三层：指标 + 降级 + 探活

### 1. Prometheus 指标（`core/metrics.py`）
- **Counter**：REQUESTS / QA_TOTAL / LLM_CALLS / EMBED_CALLS / RERANK_CALLS / FEEDBACK / CACHE_HIT[layer] / DEGRADED[tag] / CRAG_GRADE / SAFETY_BLOCK / DOMAIN_CALLS / ROUTING_DECISION / REWRITE_IMPROVED[strategy] ...
- **Gauge**：KB_DOCS/CHUNKS/VECTORS / COMPONENT_HEALTH[component] / CACHE_MYSQL_ROWS
- **Histogram**：LATENCY / RETRIEVAL_LATENCY / LLM_LATENCY / HALLUC / AGENT_ITERS

### 2. init_metric_series（预注册 0 值，消除面板 No data）
prometheus_client 指标**未触碰前不输出**该 label 序列 → Grafana "No data" 像没打通。启动时把已知小基数 label（FEEDBACK like/dislike、CACHE_HIT redis/mysql/semantic/llm、CRAG grade/action、组件健康等）预置 `.inc(0)`。开放基数（ERRORS.code/DEGRADED.tag）由 Grafana `or vector(0)` 兜底。

### 3. 进程内 mirror（`cache_hit_inc` / `cache_hit_rate`）
prometheus Counter 进程内**无法直接读值**（只能抓 /metrics 文本）。优化建议面板要实时命中率 → `cache_hit_inc(layer)` 同时写 prometheus + 进程内 dict，`cache_hit_rate()` 读 dict 算。

### 4. degraded（`core/obs.py`，盲降级显形）
原 `except: pass` 改为 `degraded(tag, e)`：记 `DEGRADED.labels(tag).inc()` + warning 日志。Grafana 看 `grid_degraded_total{tag}` 定位静默退化（百炼欠费→rerank 挂、Neo4j 没启→图谱降级、Redis 挂→缓存失效）。

### 5. Provider 健康探活
- `/system/health/providers`：主动 ping LLM/embedding provider（抓 key 失效/欠费/配额耗尽）
- `/health`：配置态快照（mysql/minio/milvus/redis checks + provider keyConfigured）
- 后台 30s 周期刷 `COMPONENT_HEALTH` gauge（原只在 /health 刷新→看板常驻空值）

### 6. Grafana（22+7 面板）
HTTP/LLM/Embedding/缓存分层/路由决策/幻觉/反馈/知识库/CRAG/安全/领域/组件健康/降级... provisioning 自动建。

## 二、部署（Docker Compose 全栈编排）

### 服务（`docker-compose.yml`）
grid-backend / frontend / mysql(3307) / redis(6379) / milvus(+etcd+minio) / neo4j(7474/7687) / grafana(3000) / prometheus(9090) / nacos(8848)

### 端口
- 8001 backend（本机 8000 被 Manager.exe 占，固定 8001）
- 5173 frontend（Vite dev，HMR）
- 3307 mysql / 6379 redis / 3000 grafana / 9090 prometheus / 7474+7687 neo4j

### rebuild 规则（关键！源码 bake 进镜像，无 bind mount）
- **改后端源码** → 必须 `docker compose up -d --build backend`（镜像重建）
- **改 .env** → 必须 `up -d` 重建容器（env 进容器）
- **改前端 .vue** → Vite dev server HMR 自动热更新，**不用 rebuild**
- **改 docker-compose.yml**（如 redis maxmemory）→ `up -d <service>` 重建该容器

> 忘了 rebuild 是最常见坑（改了代码没生效）。前端 dev server 是本地起的（非容器），改完即生效。

### Alembic 迁移
- `alembic revision --autogenerate` + `upgrade head` 建表
- 开发环境用 `Base.metadata.create_all` 兜底（alembic 不在 PATH 时）
- 手动迁移要确认 `down_revision` 指向当前 head

### Redis 配置
- `command: redis-server --appendonly yes --maxmemory 300mb --maxmemory-policy allkeys-lru --maxmemory-samples 10`
- AOF 持久化 + LRU 淘汰 + 300MB 上限

## 三、配置（`config.py` + `.env`）
- pydantic-settings 读 `.env`，`Settings` 单例
- `.env.example` 与 config.py 字段一一对应（部署不踩坑）
- 运行时可调：`/system/config/milvus`（ef）/ `/system/config/model`（temperature）→ 存 Redis + 内存热读（`config_service.load_runtime`）
