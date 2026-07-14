# 异步任务队列设计（RQ + Redis）

- **日期**: 2026-07-14
- **状态**: 待用户复核
- **关联**: 替换散落的 `asyncio.create_task` / `ensure_future` + `while True` 周期 loop；为后续「A/B 实验平台」（独立 spec）提供"评测异步执行"地基
- **技术栈**: Redis Queue (RQ) + rq-scheduler + rq（复用现有 Redis，独立 db 号做 broker）

---

## 1. 背景与动机

电网运维 RAG 系统当前后台任务以 `asyncio.create_task` / `ensure_future` fire-and-forget 方式散落于 7 个文件 11+ 处，外加 6+ 个 `while True` 周期 loop。三类问题：

1. **进程重启即丢**：kg 抽取、LLM-judge 评测、证据缺口收集、告警处置、成本记录等后台任务无持久化。后端重启（发版/崩溃/`--reload`）时未完成任务直接丢失 → 图谱残缺、评测缺数据、成本漏记。
2. **无背压限流**：并发上传 N 篇文档 → N 个 kg 抽取 LLM 调用同时触发，无并发上限，可能打爆 LLM provider 配额（百炼欠费历史问题会加剧）。
3. **周期任务散兵游勇**：`main.py` 4 个 `create_task` + `cache_persist`/`log_archive`/`evidence_gap`/`system`/`qa` 各一个 `while True`，无统一调度源，无法暂停/调参/可观测。

## 2. 目标与非目标

**目标**
- **G1 持久化**：后台任务进 Redis broker，worker 独立进程消费，进程重启不丢。
- **G2 背压**：LLM 类任务并发上限（`default` 队列 = 3），防打爆配额。
- **G3 统一调度**：6+ 周期 loop 收口进 rq-scheduler，单一调度源、可暂停可调参。
- **G4 可见性**：admin 任务监控面板（队列水位/死信/重投），接 RBAC。
- **G5 灰度可回退**：`RQ_ENABLED` 总开关，False 时 `enqueue()` 回退原 `create_task` 行为，零影响。

**非目标**
- 不做分布式多机 worker（单机多进程足够当前规模）。
- 不重写现有 async service（thin wrapper 适配，复用所有 `async def`）。
- 不包含 A/B 实验平台（独立 spec）。

## 3. 现状清单（迁移源）

**fire-and-forget 站点**（grep `create_task|ensure_future|_bg_tasks`）：

| 文件 | 站点 | 迁入队列 |
|---|---|---|
| `document_service.py` | `_kg_extract_bg` | default |
| `qa_service.py` | `record_token_usage` ×2、`eval_quality` ×2、`evidence_gap.collect` ×2 | low / default / default |
| `feedback_service.py` | `_judge_bg`（dislike 触发 LLM judge） | default |
| `alert_disposal_service.py` | `_run_disposal` | default |
| `agent_runtime.py` | `log_tool_call`（工具审计） | low |
| `routers/qa.py` | `invalidate_cache_on_dislike`、`maybe_blacklist_on_dislike` | realtime |
| `evidence_gap_service.py` | `_run` loop（AI 草稿生成） | default |

**周期 loop**（迁入 rq-scheduler）：

| 位置 | 周期 | 调度方式 |
|---|---|---|
| `main.py` 组件健康探活 | 30s | `interval=30` |
| `main.py` metrics 刷新 | ~15s | `interval=15` |
| `main.py` 缓存清理/淘汰 | 周期 | `cron` 每小时 |
| `cache_persist.py` ×2 | 周期 | `cron` 每 10 分钟 |
| `log_archive_service.py` | 24h | `cron` 每日 03:00 |
| `system.py` / `qa.py` 内 loop | 周期 | `cron`/`interval` 按原频次 |

## 4. 架构设计

### 4.1 部署拓扑

```
┌────────────────────────────────────────────────────────────┐
│  grid-backend  (FastAPI)                                    │
│  qa_service / document_service / feedback_service …         │
│       │  await enqueue('default', 'kg_extract', doc_id=…)   │
│       ▼   (RQ_ENABLED=False → 自动回退 asyncio.create_task) │
│   app/tasks/registry.py  ────────►  Redis (db=2 = broker)   │
│   (统一 enqueue 入口)            (与缓存 db=0 隔离)          │
└────────────────────────────────────────────────────────────┘
                              │
          ┌───────────────────┬────────────────────┐
          ▼                   ▼                    ▼
   grid-worker-realtime  grid-worker-default  grid-worker-low    grid-scheduler
   (scale=8)             (scale=3)            (scale=2)          rqscheduler
   rq worker realtime    rq worker default    rq worker low      (周期 cron/interval
   ───── 每队列独立 worker 服务，实例数=并发度；均复用 backend 镜像(command override) ─────
```

两个新 docker 服务 **command-override 复用 backend 镜像**，不新建镜像。

### 4.2 队列与任务映射

| 队列 | worker 实例数 | 任务 | 理由 |
|---|---|---|---|
| `realtime` | 8 | dislike 缓存失效、dislike 黑名单 | 用户在等反馈生效，要快 |
| `default` | **3** | kg 三元组抽取、LLM-judge 评测、证据缺口收集、证据缺口 AI 草稿、告警处置、feedback judge | LLM 重计算，**低并发背压防打爆配额** |
| `low` | 2 | token 成本记录、agent 工具审计、日志归档 | 数据落盘/审计，容忍延迟 |

### 4.3 数据流（以"上传文档 → 图谱抽取"为例）

```
document_service.vectorize_document()
   │  原: _bg_tasks.add(create_task(_kg_extract_bg(doc_id)))   ← 重启即丢
   │  新: await enqueue('default', 'kg_extract', doc_id=doc_id)
   ▼
registry.enqueue → default_queue.enqueue(handlers.kg_extract, doc_id) → 序列化进 Redis(db=2)
   ▼
grid-worker 取出 → handlers.kg_extract(doc_id)   # thin sync wrapper
   ▼
asyncio.run(_kg_extract_async(doc_id))           # 复用现有 async service
   ├ 成功 → FinishedJob（TTL 自动清）
   ├ 异常 → RQ 自动重试（10/30/60s）
   └ 3 次失败 → FailedJobRegistry（死信，admin 可重投/丢弃）
```

### 4.4 async 适配（thin wrapper，零重写 service）

现有后台函数几乎都是 `async def`，RQ 任务为同步模型 → 一层 thin wrapper 适配，**不重写任何 service**：

```python
# app/tasks/handlers.py  —— 全部模块级函数（RQ 要求可被 worker 顶层 import）
import asyncio
from app.db.session import AsyncSessionLocal

def kg_extract(doc_id: str):              # RQ 调用的同步入口
    asyncio.run(_kg_extract_async(doc_id))

async def _kg_extract_async(doc_id):      # 复用现有 async service，不动它
    async with AsyncSessionLocal() as db:
        from app.services import kg_service
        await kg_service.extract_triples(db, doc_id)
```

> 现有 `_kg_extract_bg` 是 `document_service` 内的闭包式函数，必须**提到 `app/tasks/` 模块级**才能被 RQ 序列化引用——这是改造里唯一的"结构挪动"，非重写。

**执行模型（实现者必读）**：RQ 每个 job 由 worker 进程 **fork 一个子进程**执行，`asyncio.run()` 在该子进程内起独立 event loop 调 async service。
- 优点：job 间进程级隔离，单 job 崩溃（OOM/段错误）不拖垮 worker；
- 代价：每 job 有 fork + import + loop 启动开销（~数百 ms），**不适合高频微任务**；
- 结论：本项目后台任务均为重 LLM/IO 任务（秒级以上），开销可忽略。

> 并发模型澄清：RQ **单 worker 进程一次只处理 1 个 job**（非 Celery 的 `--concurrency=N`）。并发靠**多 worker 进程实例**——故每队列起独立 worker 服务、用 docker `scale` 控实例数（realtime=8 / default=3 / low=2）。配置项 `RQ_CONCURRENCY_*` = 该队列 worker 实例数。

## 5. 重试 / 死信 / 幂等

### 5.1 重试分级（不一刀切）

| 队列/类型 | 重试次数 | 退避 | 死信 |
|---|---|---|---|
| `default`·LLM 类 | 3 | 10/30/60s | 3 次失败 → FailedJobRegistry |
| `realtime`·缓存失效/黑名单 | 1 | 10s | 快速失败，用户可重试触发 |
| `low`·成本/审计/归档 | 2 | 30/60s | 进死信，admin 人工 |

任务体最外层一律 `try/except degraded(tag, e)`，保持现有降级可见性（Grafana 第 18 面板），不静默。

### 5.2 幂等性约束（队列引入后必须正面处理，重试 = 重复执行）

| 任务 | 幂等性 | 处理 |
|---|---|---|
| `kg_extract` | 天然幂等（清旧写新） | 放心重试 |
| `eval_quality` | **非幂等**（重复写多条评测记录） | `(query_hash, ts_bucket)` upsert 去重 |
| `evidence_gap.collect` | 已有 `query+ts` 去重 | 复查确认去重逻辑 |
| `record_token_usage` | **非幂等**（成本累加，重试会重复计费） | 改为"先查当日已记 → upsert"，禁止简单累加 |
| `invalidate_cache_on_dislike` | 天然幂等（删缓存） | 放心重试 |
| `_run_disposal`（告警处置） | 复查 | 按 disposal_id 去重，避免重复处置 |
| `_judge_bg`（feedback LLM judge） | **非幂等**（重复写 judge 结果） | 按 feedback_id upsert，重试覆盖而非新增 |
| 证据缺口 AI 草稿 | **非幂等**（重复生成草稿） | 按 gap_id upsert ai_draft，重试覆盖 |

## 6. 可见性（Admin 任务监控，套餐 A 核心）

### 6.1 后端 `/system/tasks`（全部 `require_perm`，新增 `RQ_VIEW` / `RQ_MANAGE` 权限点，接 RBAC 矩阵 + `perm.js` 镜像）

| 端点 | 作用 | 实现 |
|---|---|---|
| `GET /system/tasks/overview` | 3 队列 waiting/active/failed/deferred 计数 | `Queue.count` + `StartedJobRegistry` + `FailedJobRegistry` |
| `GET /system/tasks/failed?queue=&page=` | 死信明细(id/func/args/error/retry) | `FailedJobRegistry.get_job_ids()` |
| `POST /system/tasks/{job_id}/requeue` | 重投死信 | `job.requeue()`（需 `RQ_MANAGE`） |
| `DELETE /system/tasks/{job_id}` | 丢弃死信 | `job.delete()`（需 `RQ_MANAGE`） |
| `GET /system/tasks/scheduled` | rq-scheduler 周期任务 + 下次执行时间 | 读 `rq:scheduler:scheduled_jobs` |

**权限角色分配**（写入 `core/permissions.py` ROLE_PERMISSIONS + Alembic 迁移）：`RQ_VIEW` → admin / auditor；`RQ_MANAGE`（重投/丢弃）→ admin。

### 6.2 前端
`Admin.vue` 加「任务监控」tab：3 队列状态卡片（颜色徽章）+ 死信表（重投/丢弃按钮）+ 周期任务下次执行时间。

### 6.3 metrics
新增 `grid_rq_jobs{queue,status}` Gauge（waiting/active/failed/deferred），worker 心跳周期 `set` → Grafana 第 19 面板「异步任务水位」。`init_metric_series()` 预注册 0 值（守 [[grafana-monitoring]] 坑①）。

## 7. 配置项（config.py + .env.example 全量对齐，守 P0-2 原则）

```
RQ_ENABLED=false              # 总开关，False 时 enqueue 回退 create_task（灰度关键）
RQ_REDIS_DB=2                 # broker 独立 db，与缓存 db=0 隔离
RQ_CONCURRENCY_REALTIME=8     # = 该队列 grid-worker 实例数（docker scale）
RQ_CONCURRENCY_DEFAULT=3      # LLM 背压；= worker 实例数
RQ_CONCURRENCY_LOW=2
RQ_RETRY_MAX=3
RQ_RETRY_INTERVAL=10,30,60
RQ_JOB_TTL=600                # 完成 job 保留时长(s)
RQ_SCHEDULER_INTERVAL=60
```

`docker-compose.yml`：`grid-backend` 环境变量加以上；新增 `grid-worker` / `grid-scheduler` 服务定义（command override）。

## 8. 灰度迁移（5 批，每批独立验证、可独立回退）

`RQ_ENABLED` 总开关 → `enqueue()` 双路，**任何时候切回 False 都零影响**：

```python
async def enqueue(queue: str, func_name: str, **kwargs):
    if not settings.RQ_ENABLED:
        _bg_tasks.add(asyncio.create_task(_run_legacy(func_name, **kwargs)))  # 回退原行为
        return None
    _queues[queue].enqueue(_HANDLERS[func_name], **kwargs)
```

| 批次 | 内容 | 风险 |
|---|---|---|
| 1 地基 | 建 `app/tasks/`(registry/handlers/scheduler) + config + docker 服务 + `/system/tasks` API + Admin tab + 单测。`RQ_ENABLED=False`，**零影响** | 低 |
| 2 default 点 | document_service(kg)、qa_service(评测/证据缺口)、evidence_gap、alert_disposal、feedback(judge) 改 enqueue | 中（LLM 幂等） |
| 3 realtime+low 点 | routers/qa(dislike×2)、qa(成本)、agent_runtime(审计)、log_archive 改 enqueue | 中（成本 upsert） |
| 4 周期收口 | rq-scheduler 注册 6+ 周期任务，**删 `main.py` 4 个 create_task + 散落 while True** | 中 |
| 5 上线 | `RQ_ENABLED=True`，盯 Grafana + 死信率，确认正常 | 低（可秒切回） |

## 9. 测试策略

- **单测 `tests/test_tasks.py`**：`enqueue` 双路分发（True/False）、handlers 调对 async service、registry 3 队列实例化、重试配置注入。
- **集成 `tests/test_rq_integration.py`**：fakeredis enqueue → 断言 job 入队；构造 raise → 断言进 FailedJobRegistry；`requeue`/`delete` 路径。（CI 默认跑，无需真 worker）
- **回归**：现有 `test_kg`/`test_feedback`/`test_qa`/`test_evidence_gap` 全绿（改造调用点不破坏 service 行为）。
- **端到端（手动）**：`docker compose up grid-worker grid-scheduler`，上传文档 → `/system/tasks` 看到 `kg_extract` → 完成/失败链路。

## 10. 风险与规避

| 风险 | 规避 |
|---|---|
| RQ Windows fork 限制 | 开发期不直接跑 worker，用 `docker compose up grid-worker`；CI 在 Linux 跑真实 worker 集成 |
| args 序列化（cloudpickle） | 任务签名**只收原始类型**（doc_id 等 str/int），禁传 db session / 不可 pickle 对象 |
| event loop 复用 | 每 task `asyncio.run` 起新 loop（独立后台任务本就该隔离）；`engine` 是模块级单例不重建 |
| 30s 周期 | rq-scheduler `interval=30`（秒级）；cron 仅用于分钟级及以上任务 |
| Redis broker 与缓存冲突 | 独立 `RQ_REDIS_DB=2`，key 前缀 RQ 默认 `rq:` |
| 每 job fork 开销 | 重 LLM/IO 任务可忽略；realtime 轻任务由 dislike 低频触发，可接受 |
| docker `scale` 非 swarm 模式 | compose v2 `deploy.replicas` 在 `docker compose up` 生效；或用 `--scale grid-worker-default=3` |

## 11. 验收标准

- [ ] `RQ_ENABLED=false` 时，全部现有行为不变，回归测试全绿。
- [ ] `RQ_ENABLED=true` 时，上传文档触发 kg 抽取 → `/system/tasks` 可见任务 → worker 消费完成。
- [ ] 构造失败任务 → 重试 3 次 → 进死信 → admin 重投成功。
- [ ] 周期任务由 rq-scheduler 调度，`main.py` 无残留 `create_task`/`while True`（4 处 + 散落）。
- [ ] `grid_rq_jobs` 指标进 Grafana 第 19 面板，死信率可观测。
- [ ] 并发上传 10 文档，kg 抽取 LLM 调用并发 ≤ 3（背压生效）。
- [ ] `/system/tasks` 仅 `RQ_VIEW`/`RQ_MANAGE` 角色可访问（RBAC 验证）。
