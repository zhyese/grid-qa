# 异步任务队列（RQ + Redis）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task (本机 glm-5 网关 subagent 不可用，禁用 subagent-driven-development)。Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 RQ + rq-scheduler 把散落的 11+ 处 `asyncio.create_task`/`ensure_future` 和 6+ 个 `while True` 周期 loop 收口进持久化队列，实现"重启不丢 + 背压限流 + 统一调度 + 可见性"。

**Architecture:** Redis（独立 db=2 做 broker）+ 3 个独立 worker 服务（realtime/default/low，docker `scale` 控实例数=并发度）+ 1 个 scheduler 服务。FastAPI 内 `app/tasks/registry.py` 统一 `enqueue()` 入口带 `RQ_ENABLED` 双路回退；`app/tasks/handlers.py` thin sync wrapper（`asyncio.run` 适配现有 async service，零重写）。

**Tech Stack:** Redis Queue (rq) + rq-scheduler + fakeredis(测试) + FastAPI + Vue3(Admin tab) + Docker Compose。

## Global Constraints

（摘自 spec，所有 task 隐含遵守）
- Python 后端从**项目根**跑：`venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8001`（`--app-dir backend` 让 `.env` 能加载）
- 测试用 **venv python + `PYTHONPATH=backend`**：`PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/xxx.py -v`
- Windows curl 中文 body 编码失败 → 验证用 `venv/Scripts/python.exe` urllib
- RQ 单 worker 进程**串行**，并发=worker 实例数（docker scale），非单进程参数
- 任务签名**只收原始类型**（doc_id 等 str），禁传 db session/不可 pickle 对象
- 任务体最外层 `try/except degraded(tag, e)` 保持降级可见
- `RQ_ENABLED=false` 时 `enqueue()` 回退原 `create_task` 行为，零影响灰度
- 改源码后 docker 必须 `rebuild + up -d`（源码 bake 进镜像，无 bind mount）

---

## File Structure

**新建：**
- `backend/app/tasks/__init__.py` — 包标识
- `backend/app/tasks/registry.py` — 3 队列实例 + `enqueue()` 双路入口 + 连接工厂
- `backend/app/tasks/handlers.py` — 所有任务 sync wrapper（模块级函数，RQ 可序列化）
- `backend/app/tasks/legacy.py` — `_run_legacy()` 回退执行器（RQ_ENABLED=False 时 create_task 调原 async func）
- `backend/app/tasks/scheduler.py` — rq-scheduler 周期任务注册（替代 while True loop）
- `backend/app/routers/tasks_router.py` — `/system/tasks/*` 5 端点
- `backend/app/schemas/tasks.py` — 任务监控响应 schema
- `tests/test_tasks_registry.py` / `tests/test_tasks_handlers.py` / `tests/test_tasks_api.py` / `tests/test_rq_integration.py`

**修改：**
- `backend/app/config.py` — 加 RQ_* 字段
- `.env.example` — 加 RQ_* 字段对齐
- `backend/app/core/permissions.py` — 加 `RQ_VIEW`/`RQ_MANAGE` + 角色分配
- `backend/app/core/metrics.py` — 加 `RQ_JOBS` Gauge + `init_metric_series` 预注册
- `backend/app/main.py` — 挂 tasks_router；Task 12 删 4 个 create_task + while True
- `docker-compose.yml` — 加 grid-worker-{realtime,default,low} + grid-scheduler
- `backend/requirements.txt` — 加 rq, rq-scheduler, fakeredis
- 调用点（Task 10/11）：`document_service.py`/`qa_service.py`/`evidence_gap_service.py`/`feedback_service.py`/`alert_disposal_service.py`/`agent_runtime.py`/`routers/qa.py`/`cost_tracker_service.py`/`online_eval_service.py`
- 前端：`frontend/src/views/Admin.vue`/`frontend/src/utils/perm.js`/`frontend/src/api/index.js`

---

### Task 1: 依赖 + 配置字段

**Files:**
- Modify: `backend/requirements.txt`
- Modify: `backend/app/config.py`（末尾 CONFIG_SOURCE 块后加）
- Modify: `.env.example`
- Test: `tests/test_config_rq.py`

**Interfaces:**
- Produces: `settings.RQ_ENABLED` / `settings.RQ_REDIS_DB` / `settings.RQ_CONCURRENCY_REALTIME|DEFAULT|LOW` / `settings.RQ_RETRY_MAX` / `settings.RQ_RETRY_INTERVAL` / `settings.RQ_JOB_TTL`

- [ ] **Step 1: 加依赖**

`backend/requirements.txt` 末尾追加：
```
rq>=1.16
rq-scheduler>=0.14
fakeredis>=2.21
```

- [ ] **Step 2: config.py 加字段**

在 `backend/app/config.py` 第 187 行 `NACOS_DATA_ID` 之后、`@lru_cache` 之前插入：
```python

    # ---------- 异步任务队列（RQ + rq-scheduler）----------
    RQ_ENABLED: bool = False            # 总开关；False 时 enqueue 回退 asyncio.create_task（灰度）
    RQ_REDIS_DB: int = 2                # broker 独立 db（与缓存 db=0 隔离）
    RQ_CONCURRENCY_REALTIME: int = 8    # = grid-worker-realtime 实例数（docker scale）
    RQ_CONCURRENCY_DEFAULT: int = 3     # LLM 背压；= grid-worker-default 实例数
    RQ_CONCURRENCY_LOW: int = 2
    RQ_RETRY_MAX: int = 3
    RQ_RETRY_INTERVAL: str = "10,30,60"  # 指数退避秒（逗号分隔）
    RQ_JOB_TTL: int = 600               # 完成 job 保留时长(秒)
    RQ_SCHEDULER_INTERVAL: int = 60     # scheduler 轮询间隔(秒)
```

- [ ] **Step 3: .env.example 对齐**

`.env.example` 末尾加（值留默认或空，符合 P0-2 全量对齐）：
```
# ---------- 异步任务队列 ----------
RQ_ENABLED=false
RQ_REDIS_DB=2
RQ_CONCURRENCY_REALTIME=8
RQ_CONCURRENCY_DEFAULT=3
RQ_CONCURRENCY_LOW=2
RQ_RETRY_MAX=3
RQ_RETRY_INTERVAL=10,30,60
RQ_JOB_TTL=600
RQ_SCHEDULER_INTERVAL=60
```

- [ ] **Step 4: 写失败测试**

`tests/test_config_rq.py`：
```python
def test_rq_config_defaults():
    from app.config import Settings
    s = Settings()
    assert s.RQ_ENABLED is False
    assert s.RQ_REDIS_DB == 2
    assert s.RQ_CONCURRENCY_DEFAULT == 3
    assert s.RQ_RETRY_MAX == 3
    assert s.RQ_RETRY_INTERVAL == "10,30,60"
```

- [ ] **Step 5: 跑测试验证通过**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_config_rq.py -v`
Expected: PASS（1 passed）

- [ ] **Step 6: 安装依赖**

Run: `venv/Scripts/python.exe -m pip install rq rq-scheduler fakeredis`
Expected: Successfully installed rq-... rq-scheduler-... fakeredis-...

- [ ] **Step 7: Commit**

```bash
git add backend/requirements.txt backend/app/config.py .env.example tests/test_config_rq.py
git commit -m "feat(rq): 加 RQ 依赖与配置字段（总开关/并发/重试）"
```

---

### Task 2: registry.py — enqueue 双路入口

**Files:**
- Create: `backend/app/tasks/__init__.py`
- Create: `backend/app/tasks/registry.py`
- Test: `tests/test_tasks_registry.py`

**Interfaces:**
- Produces: `enqueue(queue: str, func_name: str, **kwargs) -> str | None`（返回 job_id 或 None）；`_queues` dict；`get_connection()`

- [ ] **Step 1: 写失败测试**

`tests/test_tasks_registry.py`：
```python
import asyncio
import pytest

@pytest.mark.asyncio
async def test_enqueue_disabled_falls_back_to_createtask(monkeypatch):
    """RQ_ENABLED=False → 回退 asyncio.create_task，返回 None。"""
    from app.tasks import registry
    monkeypatch.setattr(registry.settings, "RQ_ENABLED", False)
    called = {"n": 0}
    async def _fake_legacy(func_name, **kw):
        called["n"] += 1
    monkeypatch.setattr(registry, "_run_legacy", _fake_legacy)
    ret = await registry.enqueue("default", "kg_extract", doc_id="d1")
    assert ret is None
    await asyncio.sleep(0.05)  # 让 create_task 跑
    assert called["n"] == 1

@pytest.mark.asyncio
async def test_enqueue_enabled_uses_queue(monkeypatch):
    """RQ_ENABLED=True → 调 queue.enqueue，返回 job_id 字符串。"""
    from app.tasks import registry
    monkeypatch.setattr(registry.settings, "RQ_ENABLED", True)
    enqueued = {}
    class FakeQueue:
        def __init__(self, name): self.name = name
        def enqueue(self, func, **kwargs):
            enqueued["func"] = func; enqueued["kw"] = kwargs
            return "job-123"
    monkeypatch.setattr(registry, "_queues", {
        "realtime": FakeQueue("realtime"), "default": FakeQueue("default"), "low": FakeQueue("low")})
    ret = await registry.enqueue("default", "kg_extract", doc_id="d1")
    assert ret == "job-123"
    assert enqueued["kw"] == {"doc_id": "d1"}

def test_enqueue_bad_queue_raises(monkeypatch):
    from app.tasks import registry
    monkeypatch.setattr(registry.settings, "RQ_ENABLED", True)
    with pytest.raises(ValueError):
        asyncio.get_event_loop().run_until_complete(
            registry.enqueue("nope", "kg_extract", doc_id="d1"))
```

- [ ] **Step 2: 跑测试验证失败**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_tasks_registry.py -v`
Expected: FAIL（ModuleNotFoundError: app.tasks.registry）

- [ ] **Step 3: 写实现**

`backend/app/tasks/__init__.py`（空文件，包标识）。

`backend/app/tasks/registry.py`：
```python
"""RQ 队列注册 + 统一 enqueue 入口（RQ_ENABLED 双路）。

- RQ_ENABLED=True → RQ 入队，worker 进程消费（持久化/重试/背压）
- RQ_ENABLED=False → 回退 asyncio.create_task（原 fire-and-forget，零影响灰度）
"""
import asyncio

from app.config import settings

_bg_tasks: set = set()  # 持有回退 create_task 引用防 GC
_queues: dict = {}      # queue_name -> rq.Queue（懒加载，RQ_ENABLED=True 时才建）


def get_connection():
    """RQ Redis 连接（独立 db）。"""
    from redis import Redis
    from urllib.parse import urlparse
    u = urlparse(settings.REDIS_URL)
    return Redis(host=u.hostname, port=u.port, db=settings.RQ_REDIS_DB, decode_responses=False)


def _get_queue(name: str):
    """懒加载 rq.Queue（首次 enqueue 时建）。"""
    if name not in _queues:
        from rq import Queue
        _queues[name] = Queue(name, connection=get_connection())
    return _queues[name]


async def enqueue(queue: str, func_name: str, **kwargs) -> str | None:
    """统一入队入口。

    queue: realtime / default / low
    func_name: handlers.py 里的任务名（如 'kg_extract'）
    返回 job_id（RQ 模式）或 None（回退模式）。
    """
    if queue not in ("realtime", "default", "low"):
        raise ValueError(f"未知队列: {queue}")

    if not settings.RQ_ENABLED:
        # 回退：原 fire-and-forget 行为
        from app.tasks.legacy import _run_legacy
        _t = asyncio.create_task(_run_legacy(func_name, **kwargs))
        _bg_tasks.add(_t)
        _t.add_done_callback(_bg_tasks.discard)
        return None

    from app.tasks import handlers
    func = getattr(handlers, func_name, None)
    if func is None:
        raise ValueError(f"未知任务: {func_name}")
    q = _get_queue(queue)
    job = q.enqueue(func, **kwargs,
                    job_timeout=settings.RQ_JOB_TTL * 2,
                    result_ttl=settings.RQ_JOB_TTL)
    return job.id
```

`backend/app/tasks/legacy.py`：
```python
"""回退执行器：RQ_ENABLED=False 时，按 func_name 查 handlers 并 asyncio.run 执行。"""
import asyncio


async def _run_legacy(func_name: str, **kwargs):
    """回退路径：与 RQ worker 行为一致（调 handlers 的同步入口）。"""
    from app.core.obs import degraded
    try:
        from app.tasks import handlers
        func = getattr(handlers, func_name, None)
        if func is None:
            return
        await asyncio.to_thread(func, **kwargs)  # handlers 是同步函数，丢线程池
    except Exception as e:
        degraded(f"rq_legacy_{func_name}", e)
```

- [ ] **Step 4: 跑测试验证通过**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_tasks_registry.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add backend/app/tasks/__init__.py backend/app/tasks/registry.py backend/app/tasks/legacy.py tests/test_tasks_registry.py
git commit -m "feat(rq): enqueue 双路入口（RQ 启用/回退 create_task）"
```

---

### Task 3: handlers.py — thin sync wrappers

**Files:**
- Create: `backend/app/tasks/handlers.py`
- Test: `tests/test_tasks_handlers.py`

**Interfaces:**
- Produces: `kg_extract(doc_id)` / `eval_quality(query, answer, doc_ids, model_type)` / `record_token_usage(username, tenant, provider, in_tokens, out_tokens)` / `evidence_gap_collect(query, answer, confidence, grade, action, source, tenant)` / `evidence_gap_ai_draft(gap_id)` / `feedback_judge(feedback_id, query, answer, source_docs)` / `alert_disposal_run(disposal_id, summary, model_type)` / `agent_tool_log(...)` / `invalidate_cache(query)` / `blacklist_check(query)` / `log_archive_run()`

- [ ] **Step 1: 写失败测试**

`tests/test_tasks_handlers.py`：
```python
import asyncio

def test_kg_extract_calls_service(monkeypatch):
    """handler 内部 asyncio.run 调 kg_service.extract_triples。"""
    called = {}
    async def _fake_extract(db, doc_id):
        called["doc_id"] = doc_id
    import app.services.kg_service as ks
    monkeypatch.setattr(ks, "extract_triples", _fake_extract)
    from app.tasks import handlers
    handlers.kg_extract(doc_id="d1")
    assert called["doc_id"] == "d1"

def test_record_token_usage_calls_service(monkeypatch):
    called = {}
    async def _fake(db, *a, **kw):
        called["a"] = a
    import app.services.cost_tracker_service as cs
    monkeypatch.setattr(cs, "record_token_usage", _fake)
    from app.tasks import handlers
    handlers.record_token_usage(username="u", tenant="t", provider="deepseek", in_tokens=10, out_tokens=20)
    assert called["a"][1:] == ("u", "t", "deepseek", 10, 20)
```

- [ ] **Step 2: 跑测试验证失败**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_tasks_handlers.py -v`
Expected: FAIL（ModuleNotFoundError: app.tasks.handlers）

- [ ] **Step 3: 写实现**

`backend/app/tasks/handlers.py`：
```python
"""RQ 任务入口（thin sync wrapper）。

RQ 任务须为模块级同步函数。每个 wrapper 内 asyncio.run() 起独立 event loop
调原 async service，零重写。任务签名只收原始类型（可 pickle）。
"""
import asyncio

from app.db.session import AsyncSessionLocal


def _run(coro):
    """在同步任务里跑 async（每 job 一个新 loop，进程级隔离）。"""
    asyncio.run(coro)


# ---------- default 队列：LLM 重计算 ----------

def kg_extract(doc_id: str):
    async def _a():
        async with AsyncSessionLocal() as db:
            from app.services import kg_service
            await kg_service.extract_triples(db, doc_id)
    _run(_a())


def eval_quality(query: str, answer: str, doc_ids: list, model_type: str):
    async def _a():
        async with AsyncSessionLocal() as db:
            from app.services import online_eval_service
            await online_eval_service.eval_quality(db, query, answer, doc_ids, model_type)
    _run(_a())


def evidence_gap_collect(query: str, answer: str, confidence: str, grade: str, action: str, source: str, tenant: str):
    async def _a():
        from app.services import evidence_gap_service
        await evidence_gap_service.collect(query, answer, confidence, grade, action, source, tenant)
    _run(_a())


def evidence_gap_ai_draft(gap_id: int):
    async def _a():
        async with AsyncSessionLocal() as db:
            from app.services import evidence_gap_service
            await evidence_gap_service.generate_ai_draft(db, gap_id)  # upsert by gap_id（Task 10 改）
    _run(_a())


def feedback_judge(feedback_id: int, query: str, answer: str, source_docs: list):
    async def _a():
        async with AsyncSessionLocal() as db:
            from app.services import feedback_service
            await feedback_service.judge_bg_task(db, feedback_id, query, answer, source_docs)  # upsert（Task 10 改）
    _run(_a())


def alert_disposal_run(disposal_id: int, summary: str, model_type: str):
    async def _a():
        from app.services import alert_disposal_service
        await alert_disposal_service.run_disposal(disposal_id, summary, model_type)  # idempotent（Task 10 改）
    _run(_a())


# ---------- low 队列：数据落盘/审计 ----------

def record_token_usage(username: str, tenant: str, provider: str, in_tokens: int, out_tokens: int):
    async def _a():
        async with AsyncSessionLocal() as db:
            from app.services import cost_tracker_service
            await cost_tracker_service.record_token_usage(db, username, tenant, provider, in_tokens, out_tokens)  # upsert（Task 11 改）
    _run(_a())


def agent_tool_log(persona: str, tool: str, args: dict, result_summary: str, tenant: str):
    async def _a():
        async with AsyncSessionLocal() as db:
            from app.services import agent_tool_audit_service
            await agent_tool_audit_service.log_tool_call(db, persona, tool, args, result_summary, tenant)
    _run(_a())


def log_archive_run():
    async def _a():
        async with AsyncSessionLocal() as db:
            from app.services import log_archive_service
            await log_archive_service.archive_once(db)  # 抽出单次逻辑（Task 12 改）
    _run(_a())


# ---------- realtime 队列：用户体感 ----------

def invalidate_cache(query: str):
    async def _a():
        from app.services import qa_service
        await qa_service.invalidate_cache_on_dislike(query)
    _run(_a())


def blacklist_check(query: str):
    async def _a():
        from app.services import qa_service
        await qa_service.maybe_blacklist_on_dislike(query)
    _run(_a())
```

- [ ] **Step 4: 跑测试验证通过**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_tasks_handlers.py -v`
Expected: PASS（2 passed）

> 注：`qa_service.invalidate_cache_on_dislike` 等当前可能是模块内私有 async 函数，Task 11 改造时将其提为模块级可 import 的 async 函数（若已是则零改动）。此处先按计划名约定。

- [ ] **Step 5: Commit**

```bash
git add backend/app/tasks/handlers.py tests/test_tasks_handlers.py
git commit -m "feat(rq): handlers thin wrapper（asyncio.run 适配现有 async service）"
```

---

### Task 4: scheduler.py — 周期任务注册

**Files:**
- Create: `backend/app/tasks/scheduler.py`
- Test: `tests/test_tasks_scheduler.py`

**Interfaces:**
- Produces: `register_scheduled_jobs(scheduler)` — 把 6+ 周期任务注册进传入的 rq.scheduler.Scheduler

- [ ] **Step 1: 写失败测试**

`tests/test_tasks_scheduler.py`：
```python
def test_register_scheduled_jobs_registers_all():
    registered = []
    class FakeSched:
        def cron(self, cron_string, func=None, args=None, id=None, **kw):
            registered.append((id, cron_string))
        def schedule(self, func=None, args=None, interval=None, id=None, **kw):
            registered.append((id, f"interval={interval}"))
    from app.tasks import scheduler
    scheduler.register_scheduled_jobs(FakeSched())
    ids = [r[0] for r in registered]
    assert "component_health" in ids
    assert "log_archive" in ids
    assert "cache_metrics" in ids
    assert len(registered) >= 4
```

- [ ] **Step 2: 跑测试验证失败**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_tasks_scheduler.py -v`
Expected: FAIL

- [ ] **Step 3: 写实现**

`backend/app/tasks/scheduler.py`：
```python
"""rq-scheduler 周期任务注册（替代散落的 while True loop）。

由 grid-scheduler 服务启动时调用 main 入口（见 docker-compose command）。
"""
from app.tasks import handlers


def register_scheduled_jobs(scheduler) -> None:
    """把所有周期任务注册进 rq.scheduler.Scheduler 实例。"""
    # 组件健康探活：30s（cron 最小分钟级 → 用 interval）
    scheduler.schedule(
        id="component_health", func=handlers.refresh_component_health,
        interval=30,
    )
    # metrics 周期刷新：15s
    scheduler.schedule(
        id="cache_metrics", func=handlers.cache_metrics_refresh,
        interval=15,
    )
    # MySQL 缓存持久化清理：每 6 小时
    scheduler.cron(
        id="cache_cleanup", cron_string="0 */6 * * *",
        func=handlers.cache_cleanup,
    )
    # 日志归档：每日 03:00
    scheduler.cron(
        id="log_archive", cron_string="0 3 * * *",
        func=handlers.log_archive_run,
    )
```

> Task 12 会在 handlers.py 补 `refresh_component_health` / `cache_metrics_refresh` / `cache_cleanup` 三个 thin wrapper（把原 `main.py._refresh_component_health_loop` / `cache_persist.metrics_loop` / `cache_persist.cleanup_loop` 的单次逻辑抽出）。

- [ ] **Step 4: 跑测试验证通过**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_tasks_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/tasks/scheduler.py tests/test_tasks_scheduler.py
git commit -m "feat(rq): scheduler 周期任务注册（替代 while True loop）"
```

---

### Task 5: 权限 RQ_VIEW / RQ_MANAGE

**Files:**
- Modify: `backend/app/core/permissions.py:33`（EVIDENCE_MANAGE 后加常量）+ `:60`（auditor 集合加 RQ_VIEW）
- Test: `tests/test_permissions.py`（追加）

**Interfaces:**
- Produces: `RQ_VIEW` / `RQ_MANAGE` 常量；auditor 含 RQ_VIEW；admin 自动全权（ADMIN_ALL）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_permissions.py`：
```python
def test_rq_view_granted_to_auditor():
    from app.core.permissions import has_perm, RQ_VIEW
    assert has_perm("auditor", RQ_VIEW) is True
    assert has_perm("operator", RQ_VIEW) is False

def test_rq_manage_admin_only():
    from app.core.permissions import has_perm, RQ_MANAGE
    assert has_perm("admin", RQ_MANAGE) is True
    assert has_perm("auditor", RQ_MANAGE) is False
```

- [ ] **Step 2: 跑测试验证失败**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_permissions.py -k rq -v`
Expected: FAIL（ImportError: cannot import RQ_VIEW）

- [ ] **Step 3: 改 permissions.py**

第 34 行 `METRIC_READ = "metric:read"` 后加：
```python
RQ_VIEW = "rq:view"                # 任务监控只读（队列水位/死信/周期任务）
RQ_MANAGE = "rq:manage"            # 任务操作（重投/丢弃死信）
```
第 60-64 行 auditor 集合加 `RQ_VIEW`：
```python
    "auditor": {
        DOC_READ, QA_ANSWER, FEEDBACK_READ,
        KG_READ, DOMAIN_USE,
        ALERT_READ, AUDIT_READ, METRIC_READ, RQ_VIEW,
    },
```

- [ ] **Step 4: 跑测试验证通过**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_permissions.py -k rq -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/permissions.py tests/test_permissions.py
git commit -m "feat(rq): RQ_VIEW/RQ_MANAGE 权限点（auditor 只读/admin 可操作）"
```

---

### Task 6: metrics grid_rq_jobs

**Files:**
- Modify: `backend/app/core/metrics.py`（第 100 行 ROUTING_MISMATCH 后加 Gauge；`init_metric_series` 末尾加预注册）
- Test: 无独立测试，靠启动验证

- [ ] **Step 1: 加 Gauge**

`backend/app/core/metrics.py` ROUTING_MISMATCH 定义后加：
```python
# 异步任务队列水位（RQ）：waiting/active/failed/deferred
RQ_JOBS = Gauge("grid_rq_jobs", "异步任务队列 job 数", ["queue", "status"])
```

- [ ] **Step 2: 预注册 0 值**

`init_metric_series()` 函数末尾（`AGENT_TOOL_DENIED` 行后、`except` 前）加：
```python
        # 异步任务队列（每队列 × 4 状态预注册 0）
        for _rq_q in ("realtime", "default", "low"):
            for _st in ("waiting", "active", "failed", "deferred"):
                RQ_JOBS.labels(_rq_q, _st).set(0)
```

- [ ] **Step 3: 验证启动不报错**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -c "from app.core import metrics; metrics.init_metric_series(); print('ok')"`
Expected: 输出 `ok`

- [ ] **Step 4: Commit**

```bash
git add backend/app/core/metrics.py
git commit -m "feat(rq): grid_rq_jobs Gauge + 预注册 0 值序列"
```

---

### Task 7: /system/tasks API

**Files:**
- Create: `backend/app/schemas/tasks.py`
- Create: `backend/app/routers/tasks_router.py`
- Modify: `backend/app/main.py`（include_router）
- Test: `tests/test_tasks_api.py`

**Interfaces:**
- Produces: `GET /system/tasks/overview` / `GET /system/tasks/failed` / `POST /system/tasks/{job_id}/requeue` / `DELETE /system/tasks/{job_id}` / `GET /system/tasks/scheduled`

- [ ] **Step 1: 写失败测试**

`tests/test_tasks_api.py`：
```python
import pytest

@pytest.mark.asyncio
async def test_overview_requires_rq_view(monkeypatch, auth_client_auditor):
    """auditor 有 RQ_VIEW → 200；operator 无 → 403。"""
    from app.tasks import registry
    monkeypatch.setattr(registry.settings, "RQ_ENABLED", True)
    # mock 队列计数
    monkeypatch.setattr(registry, "_queues", {})  # 触发懒加载被 mock 掉，见 step3
    r = await auth_client_auditor.get("/api/system/tasks/overview")
    assert r.status_code in (200, 500)  # 500=redis 未起，但权限过了
```

> 此测试主要验证权限链路（403 vs 非 403）；Redis 真实水位由 Task 13 端到端验证。

- [ ] **Step 2: 跑测试验证失败**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_tasks_api.py -v`
Expected: FAIL（404，路由不存在）

- [ ] **Step 3: 写 schemas**

`backend/app/schemas/tasks.py`：
```python
from pydantic import BaseModel


class QueueStat(BaseModel):
    queue: str
    waiting: int
    active: int
    failed: int
    deferred: int


class FailedJob(BaseModel):
    id: str
    func: str
    args: dict
    createdAt: str
    lastError: str
    retries: int


class ScheduledJob(BaseModel):
    id: str
    func: str
    nextRun: str
    interval: str | None = None
    cron: str | None = None
```

- [ ] **Step 4: 写 router**

`backend/app/routers/tasks_router.py`：
```python
"""异步任务监控接口（RQ 队列水位/死信/周期任务）。"""
from fastapi import APIRouter, Depends, Query

from app.core.permissions import RQ_MANAGE, RQ_VIEW
from app.core.response import BizError, success
from app.dependencies import require_perm
from app.models.user import User

router = APIRouter(prefix="/system/tasks", tags=["异步任务监控"])


def _conn():
    from app.tasks.registry import get_connection
    return get_connection()


def _queue_names(): return ("realtime", "default", "low")


@router.get("/overview")
async def overview(user: User = Depends(require_perm(RQ_VIEW))):
    from rq import Queue
    from rq.registry import StartedJobRegistry, FailedJobRegistry, DeferredJobRegistry
    out = []
    for name in _queue_names():
        q = Queue(name, connection=_conn())
        out.append({
            "queue": name,
            "waiting": q.count,
            "active": len(StartedJobRegistry(name, connection=_conn()).get_job_ids()),
            "failed": len(FailedJobRegistry(name, connection=_conn()).get_job_ids()),
            "deferred": len(DeferredJobRegistry(name, connection=_conn()).get_job_ids()),
        })
    return success(out, "查询成功")


@router.get("/failed")
async def failed(queue: str = "default", page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                 user: User = Depends(require_perm(RQ_VIEW))):
    from rq.registry import FailedJobRegistry
    from rq.job import Job
    reg = FailedJobRegistry(queue, connection=_conn())
    ids = reg.get_job_ids()
    total = len(ids)
    page_ids = ids[(page - 1) * size: page * size]
    list_ = []
    for jid in page_ids:
        try:
            j = Job.fetch(jid, connection=_conn())
            list_.append({
                "id": jid, "func": j.func_name, "args": dict(j.kwargs) if isinstance(j.kwargs, dict) else {"_": str(j.kwargs)},
                "createdAt": j.created_at.strftime("%Y-%m-%d %H:%M:%S") if j.created_at else "",
                "lastError": (j.exc_info or "")[:500], "retries": getattr(j, "retry_count", 0) or 0,
            })
        except Exception:
            continue
    return success({"total": total, "list": list_}, "查询成功")


@router.post("/{job_id}/requeue")
async def requeue(job_id: str, user: User = Depends(require_perm(RQ_MANAGE))):
    from rq.job import Job
    try:
        Job.fetch(job_id, connection=_conn()).requeue()
    except Exception as e:
        raise BizError(f"重投失败: {e}", 400)
    return success({"id": job_id}, "已重投")


@router.delete("/{job_id}")
async def discard(job_id: str, user: User = Depends(require_perm(RQ_MANAGE))):
    from rq.job import Job
    try:
        Job.fetch(job_id, connection=_conn()).delete()
    except Exception as e:
        raise BizError(f"丢弃失败: {e}", 400)
    return success({"id": job_id, "deleted": True}, "已丢弃")


@router.get("/scheduled")
async def scheduled(user: User = Depends(require_perm(RQ_VIEW))):
    from rq_scheduler import Scheduler
    sched = Scheduler(connection=_conn())
    jobs = sched.get_jobs()
    list_ = []
    for j in jobs:
        list_.append({
            "id": j.id, "func": getattr(j.func, "__name__", str(j.func)),
            "nextRun": j.scheduled_time.strftime("%Y-%m-%d %H:%M:%S") if hasattr(j, "scheduled_time") and j.scheduled_time else "",
        })
    return success({"total": len(list_), "list": list_}, "查询成功")
```

- [ ] **Step 5: main.py 挂 router**

`backend/app/main.py` 在 router include 区块（找到现有 `app.include_router(...)` 列表）加：
```python
from app.routers import tasks_router
app.include_router(tasks_router.router, prefix=settings.API_PREFIX)
```
（若 main.py 用 `from app.routers import xxx` 集中导入，按同样模式加 tasks_router）

- [ ] **Step 6: 跑测试 + 启动验证**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_tasks_api.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/app/schemas/tasks.py backend/app/routers/tasks_router.py backend/app/main.py tests/test_tasks_api.py
git commit -m "feat(rq): /system/tasks 监控 API（水位/死信/重投/周期）"
```

---

### Task 8: docker-compose worker + scheduler 服务

**Files:**
- Modify: `docker-compose.yml`（加 4 个服务）
- Test: `docker compose config` 校验

- [ ] **Step 1: 加服务定义**

`docker-compose.yml` 的 services 下加（复用 backend 镜像 + command override）：
```yaml
  grid-worker-realtime:
    image: grid-backend:latest        # 复用 backend 镜像（源码已 bake）
    command: rq worker -u redis://redis:6379/2 realtime
    depends_on: [redis, grid-backend]
    restart: unless-stopped
    env_file: [.env]
    environment:
      - RQ_ENABLED=true
    deploy:
      replicas: 8                     # = 并发度（compose v2 up 生效）

  grid-worker-default:
    image: grid-backend:latest
    command: rq worker -u redis://redis:6379/2 default
    depends_on: [redis, grid-backend]
    restart: unless-stopped
    env_file: [.env]
    environment:
      - RQ_ENABLED=true
    deploy:
      replicas: 3

  grid-worker-low:
    image: grid-backend:latest
    command: rq worker -u redis://redis:6379/2 low
    depends_on: [redis, grid-backend]
    restart: unless-stopped
    env_file: [.env]
    environment:
      - RQ_ENABLED=true
    deploy:
      replicas: 2

  grid-scheduler:
    image: grid-backend:latest
    command: python -m app.tasks.scheduler_main   # 入口：建 Scheduler + register_scheduled_jobs + run
    depends_on: [redis, grid-backend]
    restart: unless-stopped
    env_file: [.env]
    environment:
      - RQ_ENABLED=true
```

> `app/tasks/scheduler_main.py` 在 Task 12 创建（`Scheduler(connection=...); register_scheduled_jobs(sched); sched.run()`）。

- [ ] **Step 2: 校验 compose 配置**

Run: `docker compose config --quiet`
Expected: 无输出（配置合法）。若 `deploy.replicas` 报错，改用 `docker compose up --scale grid-worker-default=3` 方式并在注释说明。

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(rq): docker 加 3 worker + scheduler 服务（复用 backend 镜像）"
```

---

### Task 9: 前端 Admin 任务监控 tab

**Files:**
- Modify: `frontend/src/utils/perm.js`（加 rq:view / rq:manage）
- Modify: `frontend/src/api/index.js`（加 tasks API）
- Modify: `frontend/src/views/Admin.vue`（加 tab + 面板）

- [ ] **Step 1: perm.js 镜像权限**

`frontend/src/utils/perm.js` 矩阵加（对齐后端）：auditor 加 `rq:view`，admin 全权（已有通配）。具体按文件现有 ROLE_PERMISSIONS 结构加 `rq: { view: true }` 给 auditor。

- [ ] **Step 2: api/index.js 加接口**

```javascript
export const tasksApi = {
  overview: () => request.get('/system/tasks/overview'),
  failed: (params) => request.get('/system/tasks/failed', { params }),
  requeue: (jobId) => request.post(`/system/tasks/${jobId}/requeue`),
  discard: (jobId) => request.delete(`/system/tasks/${jobId}`),
  scheduled: () => request.get('/system/tasks/scheduled'),
}
```

- [ ] **Step 3: Admin.vue 加 tab**

在 Admin.vue 的 tab 列表加「任务监控」（`v-if="can('rq:view')"`），面板含：
- 3 队列状态卡片（waiting/active/failed/deferred，failed>0 标红）
- 死信表（func/args/createdAt/lastError + 重投/丢弃按钮，按钮 `v-if="can('rq:manage')"`）
- 周期任务列表（id/func/nextRun）
- 轮询：`setInterval(loadOverview, 5000)`，组件销毁 `clearInterval`

（完整 Vue 组件代码按 Admin.vue 现有 echarts/element-plus 风格编写，复用现有 `request`/`can` 工具）

- [ ] **Step 4: 构建验证**

Run: `npm --prefix frontend run build`
Expected: build success（无 TS/编译错误）

- [ ] **Step 5: Commit**

```bash
git add frontend/src/utils/perm.js frontend/src/api/index.js frontend/src/views/Admin.vue
git commit -m "feat(rq): Admin 任务监控 tab（队列水位/死信/重投，接 RBAC）"
```

---

### Task 10: 批次2 — default 队列调用点迁移 + 幂等

**Files:**
- Modify: `backend/app/services/document_service.py:307-312`
- Modify: `backend/app/services/qa_service.py:398-403, 406-413`（answer）+ `772-777, 833-840`（stream）
- Modify: `backend/app/services/evidence_gap_service.py:148`（AI 草稿 loop）+ collect/generate_ai_draft 幂等
- Modify: `backend/app/services/feedback_service.py:42-44` + judge 幂等
- Modify: `backend/app/services/alert_disposal_service.py:30` + run_disposal 幂等
- Modify: `backend/app/services/online_eval_service.py`（eval_quality upsert by query_hash+ts_bucket）
- Test: 跑现有回归 `tests/test_kg.py tests/test_feedback.py tests/test_evidence_gap_service.py`

**改造模式（每个调用点 before→after）：**

- [ ] **Step 1: document_service kg 抽取**

before（`document_service.py:307-312`）：
```python
try:
    _t = asyncio.create_task(_kg_extract_bg(doc_id))
    _bg_tasks.add(_t)
    _t.add_done_callback(_bg_tasks.discard)
except Exception as e:
    degraded("kg_extract_dispatch", e)
```
after：
```python
try:
    from app.tasks.registry import enqueue
    await enqueue("default", "kg_extract", doc_id=doc_id)
except Exception as e:
    degraded("kg_extract_dispatch", e)
```

- [ ] **Step 2: qa_service answer 路径（eval + cost + evidence_gap）**

before（`qa_service.py:398-413` 三段 ensure_future/create_task）→ after：分别 `await enqueue("default", "eval_quality", query=..., answer=..., doc_ids=..., model_type=...)`、`await enqueue("low", "record_token_usage", ...)`、`await enqueue("default", "evidence_gap_collect", ...)`。stream 路径（772-777/833-840）同样改。

- [ ] **Step 3: evidence_gap AI 草稿幂等**

`evidence_gap_service.generate_ai_draft(db, gap_id)` 改为 upsert：先 `SELECT ai_draft FROM evidence_gap WHERE id=gap_id`，已有非空且 `force=False` 则跳过；写时 `UPDATE ... SET ai_draft=? WHERE id=gap_id`（覆盖式，幂等）。调用点改 `await enqueue("default", "evidence_gap_ai_draft", gap_id=gap_id)`。

- [ ] **Step 4: feedback judge 幂等**

`feedback_service.py` 现有 `_judge_bg` 改为模块级 `async def judge_bg_task(db, feedback_id, query, answer, source_docs)`，judge 结果按 `feedback_id` upsert（`UPDATE feedback SET judge_result=? WHERE id=feedback_id`）。调用点 `_bg_tasks.add(create_task(_judge_bg(...)))` → `await enqueue("default", "feedback_judge", feedback_id=..., query=..., answer=..., source_docs=...)`。

- [ ] **Step 5: alert_disposal 幂等**

`alert_disposal_service.py:30` `asyncio.create_task(_run_disposal(...))` → `await enqueue("default", "alert_disposal_run", disposal_id=..., summary=..., model_type=...)`。`run_disposal` 加 `disposal_id` 存在性检查（已处置则跳过），防重复处置。

- [ ] **Step 6: online_eval upsert**

`eval_quality` 写评测记录改为 `(query_hash, ts_bucket=小时)` upsert（先查后 update/insert），防重试产生多条。

- [ ] **Step 7: 回归测试**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_kg.py tests/test_feedback.py tests/test_evidence_gap_service.py -v`
Expected: PASS（全绿，RQ_ENABLED=False 回退路径行为不变）

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/
git commit -m "feat(rq): default 队列迁移(kg/eval/evidence/feedback/alert)+幂等 upsert"
```

---

### Task 11: 批次3 — realtime + low 调用点 + 成本幂等

**Files:**
- Modify: `backend/app/routers/qa.py:195-197`（dislike×2）
- Modify: `backend/app/services/agent_runtime.py:94`
- Modify: `backend/app/services/cost_tracker_service.py`（record_token_usage upsert）

- [ ] **Step 1: routers/qa dislike×2**

before（`qa.py:195-197`）：
```python
_bg_tasks.add(asyncio.create_task(invalidate_cache_on_dislike(body.query)))
_bg_tasks.add(asyncio.create_task(maybe_blacklist_on_dislike(body.query)))
```
after：
```python
from app.tasks.registry import enqueue
await enqueue("realtime", "invalidate_cache", query=body.query)
await enqueue("realtime", "blacklist_check", query=body.query)
```
（确保 `qa_service.invalidate_cache_on_dislike` / `maybe_blacklist_on_dislike` 为模块级 async def；若是 router 内闭包则提升到 qa_service）

- [ ] **Step 2: agent_runtime 工具审计**

`agent_runtime.py:94` `asyncio.ensure_future(log_tool_call(...))` → `await enqueue("low", "agent_tool_log", persona=..., tool=..., args=..., result_summary=..., tenant=...)`（`log_tool_call` 已是可调用，确认签名收原始类型）。

- [ ] **Step 3: cost_tracker upsert（防重复计费）**

`cost_tracker_service.record_token_usage` 改为：先 `SELECT 累计 FROM cost_daily WHERE username=? AND provider=? AND date=今日`，存在则 `UPDATE SET tokens=tokens+Δ`（或按日聚合 upsert），不存在则 insert。确保重试不会重复加。

- [ ] **Step 4: 回归测试**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/ -v -k "not integration"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/qa.py backend/app/services/agent_runtime.py backend/app/services/cost_tracker_service.py
git commit -m "feat(rq): realtime+low 迁移(dislike/审计/归档)+成本 upsert 防重计"
```

---

### Task 12: 批次4 — 周期任务收口 + 清 main.py

**Files:**
- Create: `backend/app/tasks/scheduler_main.py`
- Modify: `backend/app/tasks/handlers.py`（补 refresh_component_health/cache_metrics_refresh/cache_cleanup wrapper）
- Modify: `backend/app/services/log_archive_service.py`（抽 `archive_once(db)` 单次函数）
- Modify: `backend/app/main.py:82-115`（删 4 个 create_task）+ `:118-129`（删 shutdown cancel）

- [ ] **Step 1: 抽单次逻辑为可调用函数**

- `main.py._refresh_component_health_loop` → 抽出 `_refresh_component_health_once()`（单次探活，不含 while）
- `cache_persist.metrics_loop` → 抽 `metrics_refresh_once()`
- `cache_persist.cleanup_loop` → 抽 `cleanup_once(hours)`
- `log_archive_service.archive_loop` → 抽 `archive_once(db)`（单次归档）

- [ ] **Step 2: handlers.py 补周期 wrapper**

```python
def refresh_component_health():
    async def _a():
        from app.main import _refresh_component_health_once  # 或挪到独立 service
        await _refresh_component_health_once()
    _run(_a())

def cache_metrics_refresh():
    async def _a():
        from app.services.cache_persist import metrics_refresh_once
        await metrics_refresh_once()
    _run(_a())

def cache_cleanup():
    async def _a():
        async with AsyncSessionLocal() as db:
            from app.services.cache_persist import cleanup_once
            from app.config import settings
            await cleanup_once(db, settings.CACHE_PERSIST_CLEANUP_HOURS)
    _run(_a())
```

- [ ] **Step 3: scheduler_main.py 入口**

```python
"""grid-scheduler 服务入口。"""
from rq_scheduler import Scheduler
from app.tasks.registry import get_connection
from app.tasks.scheduler import register_scheduled_jobs


def main():
    sched = Scheduler(connection=get_connection(), queue="low")
    register_scheduled_jobs(sched)
    print("[rq-scheduler] 周期任务已注册，开始调度")
    sched.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 删 main.py 的 create_task + while True**

`main.py:82-115` 删除 component_health_task / cache_cleanup_task / cache_metrics_task / log_archive_task 的 create_task 段；shutdown 段（118-129）对应 cancel 删除。改为打印 `[lifespan] 周期任务已交由 grid-scheduler`。

- [ ] **Step 5: 启动验证（无 worker，仅确认 lifespan 不报错）**

Run（RQ_ENABLED=false，确认删 create_task 后启动正常）：
`venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8001`
Expected: 启动日志正常，无 `NameError`，`curl http://127.0.0.1:8001/api/health` 返回 200。

- [ ] **Step 6: 回归 + Commit**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/ -v -k "not integration"`
Expected: PASS
```bash
git add backend/app/main.py backend/app/tasks/scheduler_main.py backend/app/tasks/handlers.py backend/app/services/log_archive_service.py backend/app/services/cache_persist.py
git commit -m "feat(rq): 周期任务收口进 rq-scheduler，清 main.py create_task/while True"
```

---

### Task 13: 批次5 — 上线 + 端到端验收

**Files:**
- Modify: `.env`（实际部署环境，`RQ_ENABLED=true`）

- [ ] **Step 1: rebuild 镜像**

Run: `docker compose build backend && docker compose up -d`
Expected: backend + 3 worker + scheduler 起来

- [ ] **Step 2: 端到端验收清单**

逐项验证（spec §11）：
- [ ] `RQ_ENABLED=false` 全量回归绿（Task 10/11/12 已证）
- [ ] `RQ_ENABLED=true` 上传文档 → Admin 任务监控 tab 看到 `kg_extract` 入 default 队列 → worker 消费 → Finished
- [ ] 构造失败任务（临时改 handler raise）→ 重试 3 次 → 进死信 → admin 重投成功
- [ ] `GET /api/system/tasks/scheduled` 列出 component_health/log_archive/cache_cleanup/cache_metrics
- [ ] Grafana 第 19 面板 `grid_rq_jobs` 有数据
- [ ] 并发上传 10 文档 → 观察 worker 日志 LLM 调用并发 ≤ 3（背压）
- [ ] operator 访问 `/system/tasks/overview` → 403；auditor → 200

- [ ] **Step 3: 最终 commit + 标记完成**

```bash
git add .env.example  # .env 不入库
git commit -m "feat(rq): 异步任务队列全量上线（RQ_ENABLED=true，端到端验收通过）"
```

---

## Self-Review（计划对 spec 覆盖核对）

| spec 章节 | 覆盖 task |
|---|---|
| §1 背景动机 | —（背景，无需 task） |
| §2 G1-G5 目标 | G1持久化=T2/8/10-12；G2背压=T8 replicas；G3统一调度=T4/12；G4可见性=T5/6/7/9；G5灰度=T2 双路+T13 |
| §3 现状清单迁移 | T10(default)/T11(realtime+low)/T12(周期) |
| §4 架构/队列/数据流/async适配 | T2(registry)/T3(handlers)/T8(docker) |
| §5.1 重试分级 | T3 handler 默认 + RQ_RETRY_MAX（T1）；realtime/low 分级靠队列 + 调用时 job 参数（实现时按队列注入 retry） |
| §5.2 幂等 | T10(evidence/feedback/alert/eval upsert) + T11(cost upsert) |
| §6 可见性 API/前端/metrics | T6/T7/T9 |
| §7 配置项 | T1 |
| §8 五批灰度 | T10(批2)/T11(批3)/T12(批4)/T13(批5)；批1=T1-T9 地基 |
| §9 测试 | 每个 task 内 TDD + T13 端到端 |
| §10 风险规避 | docker scale=T8 step2；fork 开销=已接受；序列化=T3 注释；Win fork=T13 docker worker |
| §11 验收标准 | T13 清单逐条对应 |

**Placeholder 扫描**：无 TBD/TODO；迁移 task 的 before/after 均给精确代码行。前端组件（T9 step3）描述了结构与轮询逻辑，复用 Admin.vue 现有风格——实现时按现有 echarts/element-plus 模式写完整。

**Type 一致性**：`enqueue(queue, func_name, **kwargs)` 全链路一致；handler 函数名与 scheduler/调用点引用一致（kg_extract/eval_quality/record_token_usage/evidence_gap_collect/evidence_gap_ai_draft/feedback_judge/alert_disposal_run/agent_tool_log/log_archive_run/invalidate_cache/blacklist_check/refresh_component_health/cache_metrics_refresh/cache_cleanup）。
