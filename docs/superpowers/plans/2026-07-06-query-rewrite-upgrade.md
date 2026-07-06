# Query 改写升级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 query 改写从 toy 升级——评估闭环（RRF 分数对比）+ Redis 缓存 + Classifier（兼 adaptive）+ few-shot prompt + 前端质量可视化面板。

**Architecture:** 后端 3 个新组件（RewriteStrategyClassifier / RewriteCache / RewriteEvaluator）+ RewriteEventLogger 接入 `mixed_search` step 0，全部复用已有 RRF/Redis/milvus/embedding 设施；前端 Admin 新增「🔧 Query改写」tab 用 echarts 可视化改写质量。CRAG force 改写与 adaptive/评估解耦。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy(async) / Redis / Milvus / pytest（后端）；Vue3 + echarts（前端）；Alembic（迁移）

## Global Constraints

- 容器 Python 3.11；Win 开发端口 8001(backend)/5173(frontend dev)/3307(mysql)/6379(redis)
- 改后端源码后必须 `docker compose up -d --build backend`（源码 bake 进镜像，无 bind mount）
- 前端 Vite dev server 5173，改 `.vue` 自动热更新，无需 rebuild
- 所有降级统一走 `app.core.obs.degraded` + `metrics.DEGRADED`
- bg task 必须用独立 `AsyncSessionLocal`（不共享请求 db——记 dislike invalidate session 并发 500 教训）
- 缓存 key 前缀 `rewrite:`；TTL 默认 7 天
- TDD：先写失败测试 → 实现 → 通过 → commit
- 配置走 `app.config.settings`；新指标进 `app.core.metrics` 并在 `init_metric_series` 预注册 0 值

## File Structure

**新建（后端）：**
- `backend/app/services/rewrite_strategy.py` — Classifier：纯规则判 query 类型（口语/缩写/术语/正常），兼任 adaptive（skip）
- `backend/app/services/rewrite_cache.py` — Redis 改写结果缓存
- `backend/app/services/rewrite_evaluator.py` — 改写前后轻量检索分数对比
- `backend/app/services/rewrite_event_service.py` — 改写事件记录 + 聚合查询
- `backend/app/models/rewrite_event.py` — rewrite_event 表 model
- `backend/app/data/rewrite_fewshot.json` — 电网 few-shot 示例库（按类型）
- `backend/migrations/versions/xxxx_add_rewrite_event.py` — Alembic 迁移

**修改（后端）：**
- `backend/app/services/query_rewrite.py` — 接 Classifier+Cache+Evaluator+few-shot，保留 force 路径
- `backend/app/services/retrieval_service.py` — `mixed_search` step 0 接入；multi_query/hyde 加 Cache
- `backend/app/routers/system.py` — 加 rewrite-stats / rewrite-events 两接口
- `backend/app/config.py` — 7 个新字段
- `backend/app/core/metrics.py` — 3 计数器 + 预注册

**新建（前端）：**
- `frontend/src/views/Admin.vue` — 新增「🔧 Query改写」tab + 面板组件（复用 echarts）

**测试：**
- `tests/test_rewrite_strategy.py` / `test_rewrite_cache.py` / `test_rewrite_evaluator.py` / `test_rewrite_event.py` / `test_rewrite_integration.py`

---

## Task 1: 配置 + metrics 基础

**Files:**
- Modify: `backend/app/config.py`（优化建议段后）
- Modify: `backend/app/core/metrics.py`（计数器区 + init_metric_series）
- Test: 无独立测试（后续任务验证）

**Interfaces:**
- Produces: `settings.REWRITE_CACHE_TTL` / `REWRITE_EVAL_ENABLE` / `REWRITE_ADAPTIVE_ENABLE` / `REWRITE_EVAL_MARGIN` / `REWRITE_EVAL_CAND` / `REWRITE_EVAL_TOPK` / `REWRITE_EVENT_SAMPLE_RATE`；`metrics.REWRITE_IMPROVED` / `REWRITE_CACHE_HIT` / `REWRITE_EVAL_REJECTED`

- [ ] **Step 1: config.py 加字段**

在 `backend/app/config.py` 的 `OPTIMIZER_BLACKLIST_THRESHOLD` 行后追加：

```python
    # ---------- Query 改写升级（评估闭环+缓存+adaptive）----------
    REWRITE_CACHE_TTL: int = 604800            # 改写缓存 TTL（7 天）
    REWRITE_EVAL_ENABLE: bool = True           # 评估闭环开关（False=改写后不评估，盲用）
    REWRITE_ADAPTIVE_ENABLE: bool = True       # Classifier 判正常 query 时跳过改写（False=全部改写）
    REWRITE_EVAL_MARGIN: float = 0.05          # 评估更优阈值（new > orig*(1+margin)）
    REWRITE_EVAL_CAND: int = 10                # 评估检索候选数
    REWRITE_EVAL_TOPK: int = 5                 # 评估取 top-K 算分数和
    REWRITE_EVENT_SAMPLE_RATE: float = 1.0     # 改写事件采样率（高流量可降避免写放大）
```

- [ ] **Step 2: metrics.py 加计数器**

在 `backend/app/core/metrics.py` 的 `CACHE_EVICTED` 行后追加：

```python
REWRITE_IMPROVED = Counter("grid_rewrite_improved_total", "改写被评估采纳次数", ["strategy"])
REWRITE_CACHE_HIT = Counter("grid_rewrite_cache_hit_total", "改写缓存命中次数", ["strategy"])
REWRITE_EVAL_REJECTED = Counter("grid_rewrite_eval_rejected_total", "改写被评估否决次数", ["strategy"])
```

- [ ] **Step 3: init_metric_series 预注册 0 值**

在 `init_metric_series()` 的 `for _reason in (...)` 块后追加：

```python
        # 改写评估（按 strategy 预注册）
        for _s in ("rewrite", "multi", "hyde"):
            REWRITE_IMPROVED.labels(_s).inc(0)
            REWRITE_CACHE_HIT.labels(_s).inc(0)
            REWRITE_EVAL_REJECTED.labels(_s).inc(0)
```

- [ ] **Step 4: 语法检查 + commit**

```bash
python -m py_compile backend/app/config.py backend/app/core/metrics.py
git add backend/app/config.py backend/app/core/metrics.py
git commit -m "feat(rewrite): config + metrics 基础（7字段+3计数器）"
```

---

## Task 2: RewriteStrategyClassifier

**Files:**
- Create: `backend/app/services/rewrite_strategy.py`
- Create: `backend/app/data/rewrite_fewshot.json`
- Test: `tests/test_rewrite_strategy.py`

**Interfaces:**
- Consumes: `term_service.normalize` / `_load_terms`（反查别名）
- Produces: `classify(query: str) -> dict` 返回 `{"type": "colloquial|abbreviation|term_missing|normal", "skip": bool, "hint": str}`；`get_fewshot(type_: str) -> list[dict]`

- [ ] **Step 1: 写失败测试** `tests/test_rewrite_strategy.py`

```python
import pytest
from app.services.rewrite_strategy import classify, get_fewshot

def test_colloquial_short_query():
    r = classify("咋办")
    assert r["type"] == "colloquial"
    assert r["skip"] is False

def test_abbreviation():
    r = classify("SF6断路器漏气怎么处理")
    assert r["type"] == "abbreviation"
    assert r["skip"] is False

def test_normal_skipped():
    r = classify("主变压器绕组温度过热的应急处置步骤")
    assert r["type"] == "normal"
    assert r["skip"] is True

def test_fewshot_returns_examples():
    fs = get_fewshot("colloquial")
    assert isinstance(fs, list) and len(fs) >= 1
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_rewrite_strategy.py -v
```
Expected: FAIL（`ModuleNotFoundError: app.services.rewrite_strategy`）

- [ ] **Step 3: 写 fewshot.json** `backend/app/data/rewrite_fewshot.json`

```json
{
  "colloquial": [
    {"q": "主变烧了咋办", "r": "主变压器故障的应急处置流程"}
  ],
  "abbreviation": [
    {"q": "SF6 漏气", "r": "SF6 断路器气体泄漏的检测与处理方法"}
  ],
  "term_missing": [
    {"q": "CT 怎么选", "r": "电流互感器(CT) 选型原则与技术参数"}
  ]
}
```

- [ ] **Step 4: 写实现** `backend/app/services/rewrite_strategy.py`

```python
"""Query 改写策略分类：判类型选 prompt+few-shot，正常 query 跳过（兼 adaptive）。"""
import json
from functools import lru_cache
from pathlib import Path

_COLLOQUIAL = {"咋", "咋办", "咋整", "啥", "啥叫", "嘛", "啥样", "咋样", "咋回事"}
_ABBR = {"CT", "PT", "SF6", "GIS", "VT", "AVR", "RTU", "SCADA", "UPS", "SVG"}


@lru_cache
def _load_fewshot() -> dict:
    p = Path(__file__).resolve().parent.parent / "data" / "rewrite_fewshot.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def get_fewshot(type_: str) -> list[dict]:
    return _load_fewshot().get(type_, [])


def classify(query: str) -> dict:
    """判 query 类型。正常→skip=True（兼 adaptive：跳过整个改写流程）。"""
    if not query or not query.strip():
        return {"type": "normal", "skip": True, "hint": "空 query"}
    q = query.strip()
    # 口语化：短 或 含口语词
    if len(q) < 8 or any(w in q for w in _COLLOQUIAL):
        return {"type": "colloquial", "skip": False, "hint": "口语化，需规范化"}
    # 缩写：含电网缩写词
    upper = q.upper()
    if any(a in upper for a in _ABBR):
        return {"type": "abbreviation", "skip": False, "hint": "含缩写，需展开"}
    # 术语缺失：含 term_service 的非标准别名
    try:
        from app.services.term_service import _load_terms
        aliases = set(_load_terms().keys())
        if any(a in q for a in aliases):
            return {"type": "term_missing", "skip": False, "hint": "含非标准术语别名"}
    except Exception:
        pass
    return {"type": "normal", "skip": True, "hint": "规范 query，无需改写"}
```

- [ ] **Step 5: 运行测试通过 + commit**

```bash
pytest tests/test_rewrite_strategy.py -v
git add backend/app/services/rewrite_strategy.py backend/app/data/rewrite_fewshot.json tests/test_rewrite_strategy.py
git commit -m "feat(rewrite): RewriteStrategyClassifier（类型分类+few-shot+兼 adaptive）"
```

---

## Task 3: RewriteCache

**Files:**
- Create: `backend/app/services/rewrite_cache.py`
- Test: `tests/test_rewrite_cache.py`

**Interfaces:**
- Consumes: `app.clients.redis_client.get_redis` / `settings.REWRITE_CACHE_TTL`
- Produces: `async get(strategy: str, query: str) -> dict | None`；`async set(strategy: str, query: str, value: dict) -> bool`

- [ ] **Step 1: 写失败测试** `tests/test_rewrite_cache.py`

```python
import pytest
from unittest.mock import AsyncMock, patch
from app.services import rewrite_cache

@pytest.mark.asyncio
async def test_set_then_get():
    with patch.object(rewrite_cache.redis_client, "get_redis") as mk:
        r = AsyncMock()
        r.set = AsyncMock(return_value=True)
        r.get = AsyncMock(return_value='{"result":"改写后","improved":true}')
        mk.return_value = r
        await rewrite_cache.set("rewrite", "q", {"result": "改写后", "improved": True})
        got = await rewrite_cache.get("rewrite", "q")
        assert got == {"result": "改写后", "improved": True}

@pytest.mark.asyncio
async def test_get_miss_returns_none():
    with patch.object(rewrite_cache.redis_client, "get_redis") as mk:
        r = AsyncMock(); r.get = AsyncMock(return_value=None); mk.return_value = r
        assert await rewrite_cache.get("rewrite", "q") is None

@pytest.mark.asyncio
async def test_get_corrupt_returns_none():
    with patch.object(rewrite_cache.redis_client, "get_redis") as mk:
        r = AsyncMock(); r.get = AsyncMock(return_value="not json"); mk.return_value = r
        assert await rewrite_cache.get("rewrite", "q") is None
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/test_rewrite_cache.py -v
```
Expected: FAIL（模块不存在）

- [ ] **Step 3: 写实现** `backend/app/services/rewrite_cache.py`

```python
"""改写结果 Redis 缓存：相同 query+strategy 不重复调 LLM。"""
import hashlib
import json

from app.clients import redis_client
from app.config import settings
from app.core.obs import degraded


def _key(strategy: str, query: str) -> str:
    h = hashlib.md5(query.encode("utf-8")).hexdigest()
    return f"rewrite:{strategy}:{h}"


async def get(strategy: str, query: str) -> dict | None:
    try:
        v = await redis_client.get_redis().get(_key(strategy, query))
        return json.loads(v) if v else None
    except Exception as e:
        degraded("rewrite_cache_get", e)
        return None


async def set(strategy: str, query: str, value: dict) -> bool:
    try:
        await redis_client.get_redis().set(
            _key(strategy, query), json.dumps(value, ensure_ascii=False),
            ex=settings.REWRITE_CACHE_TTL,
        )
        return True
    except Exception as e:
        degraded("rewrite_cache_set", e)
        return False
```

- [ ] **Step 4: 运行通过 + commit**

```bash
pytest tests/test_rewrite_cache.py -v
git add backend/app/services/rewrite_cache.py tests/test_rewrite_cache.py
git commit -m "feat(rewrite): RewriteCache（Redis 缓存，TTL 7天，异常降级）"
```

---

## Task 4: RewriteEvaluator

**Files:**
- Create: `backend/app/services/rewrite_evaluator.py`
- Test: `tests/test_rewrite_evaluator.py`

**Interfaces:**
- Consumes: `embedding_service.embed_query` / `milvus_client.search` / `settings.REWRITE_EVAL_*`
- Produces: `async evaluate(original: str, rewritten: str, model_type: str | None) -> dict` 返回 `{"improved": bool, "orig_score": float, "new_score": float}`

- [ ] **Step 1: 写失败测试** `tests/test_rewrite_evaluator.py`

```python
import pytest
from unittest.mock import AsyncMock, patch
from app.services import rewrite_evaluator

def _score_sum(hits):
    """topK 分数和的工具，测试里复刻校验。"""
    return sum(float(h.get("score", 0)) for h in (hits or [])[:5])

@pytest.mark.asyncio
async def test_improved_when_new_higher():
    # orig top5 分数和 1.0，rewritten 1.2 → improved
    fake_dense = lambda q: [{"score": 0.2}, {"score": 0.2}, {"score": 0.2}, {"score": 0.2}, {"score": 0.2}] if q == "orig" else [{"score": 0.3}]*5
    with patch.object(rewrite_evaluator, "_light_dense", AsyncMock(side_effect=fake_dense)):
        r = await rewrite_evaluator.evaluate("orig", "rewritten", None)
    assert r["improved"] is True
    assert r["orig_score"] < r["new_score"]

@pytest.mark.asyncio
async def test_reject_when_not_better():
    # 两者分数接近（< margin）→ 不更优
    same = [{"score": 0.2}] * 5
    with patch.object(rewrite_evaluator, "_light_dense", AsyncMock(return_value=same)):
        r = await rewrite_evaluator.evaluate("orig", "rewritten", None)
    assert r["improved"] is False

@pytest.mark.asyncio
async def test_exception_returns_not_improved():
    with patch.object(rewrite_evaluator, "_light_dense", AsyncMock(side_effect=Exception("boom"))):
        r = await rewrite_evaluator.evaluate("orig", "rewritten", None)
    assert r["improved"] is False
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/test_rewrite_evaluator.py -v
```
Expected: FAIL（模块不存在）

- [ ] **Step 3: 写实现** `backend/app/services/rewrite_evaluator.py`

```python
"""改写质量评估：改写前后各跑一次轻量 dense 检索，比 top-K 分数和。"""
from app.config import settings
from app.core.obs import degraded
from app.services import embedding_service
from app.clients import milvus_client
import asyncio


async def _light_dense(query: str, model_type: str | None) -> list[dict]:
    """单路 dense_cloud 轻量检索，返回 [{score}, ...]。"""
    qvec = await embedding_service.embed_query(query, settings.EMB_PROVIDER)
    return await asyncio.to_thread(
        milvus_client.search, settings.MILVUS_COLLECTION, qvec, settings.REWRITE_EVAL_CAND,
    )


def _score_sum(hits: list[dict]) -> float:
    return sum(float(h.get("score", 0) or 0) for h in (hits or [])[: settings.REWRITE_EVAL_TOPK])


async def evaluate(original: str, rewritten: str, model_type: str | None) -> dict:
    """rewritten 分数和 > original*(1+margin) 才算更优。异常回退 not improved。"""
    try:
        orig_hits, new_hits = await asyncio.gather(
            _light_dense(original, model_type),
            _light_dense(rewritten, model_type),
        )
        orig_s, new_s = _score_sum(orig_hits), _score_sum(new_hits)
        improved = new_s > orig_s * (1 + settings.REWRITE_EVAL_MARGIN)
        return {"improved": improved, "orig_score": round(orig_s, 4), "new_score": round(new_s, 4)}
    except Exception as e:
        degraded("rewrite_eval", e)
        return {"improved": False, "orig_score": 0.0, "new_score": 0.0}
```

- [ ] **Step 4: 运行通过 + commit**

```bash
pytest tests/test_rewrite_evaluator.py -v
git add backend/app/services/rewrite_evaluator.py tests/test_rewrite_evaluator.py
git commit -m "feat(rewrite): RewriteEvaluator（轻量 dense 分数对比+margin+异常回退）"
```

---

## Task 5: rewrite_event model + 迁移 + EventLogger

**Files:**
- Create: `backend/app/models/rewrite_event.py`
- Create: `backend/migrations/versions/<ts>_add_rewrite_event.py`（Alembic）
- Create: `backend/app/services/rewrite_event_service.py`
- Test: `tests/test_rewrite_event.py`

**Interfaces:**
- Consumes: `AsyncSessionLocal` / `settings.REWRITE_EVENT_SAMPLE_RATE`
- Produces: `async log(strategy, original, rewritten, improved, orig_score, new_score, cached, route)`；`async stats(period)`；`async events_page(page, size, strategy, adopted)`

- [ ] **Step 1: 写 model** `backend/app/models/rewrite_event.py`

```python
"""改写事件表：每次改写记一条，供可视化面板。"""
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RewriteEvent(Base):
    __tablename__ = "rewrite_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    strategy: Mapped[str] = mapped_column(String(16), nullable=False, index=True)   # rewrite|multi|hyde
    original_query: Mapped[str] = mapped_column(Text, nullable=False)
    rewritten_query: Mapped[str] = mapped_column(Text, nullable=False)
    improved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)       # 0/1
    orig_score: Mapped[float] = mapped_column(Float, default=0.0)
    new_score: Mapped[float] = mapped_column(Float, default=0.0)
    cached: Mapped[int] = mapped_column(Integer, default=0)                          # 0/1
    route: Mapped[str] = mapped_column(String(16), default="hybrid")
    tenant: Mapped[str] = mapped_column(String(32), default="default")
```

- [ ] **Step 2: 写 Alembic 迁移**

```bash
docker exec grid-backend alembic revision --autogenerate -m "add rewrite_event"
```
检查生成的迁移文件 `backend/migrations/versions/<hash>_add_rewrite_event.py` 含 `create_table('rewrite_event', ...)`，无其他误删。手动补 `down_revision` 链。

- [ ] **Step 3: 写失败测试** `tests/test_rewrite_event.py`

```python
import pytest
from app.services import rewrite_event_service as svc

@pytest.mark.asyncio
async def test_log_then_stats(monkeypatch):
    # 用测试库或 mock AsyncSessionLocal；这里假设有测试 fixture 提供干净 session
    monkeypatch.setattr(svc.settings, "REWRITE_EVENT_SAMPLE_RATE", 1.0)
    await svc.log("rewrite", "q", "qr", True, 0.1, 0.2, cached=False, route="hybrid")
    s = await svc.stats("today")
    assert s["total"] >= 1
    assert s["adopted"] >= 1
```

- [ ] **Step 4: 写实现** `backend/app/services/rewrite_event_service.py`

```python
"""改写事件记录 + 聚合查询（独立 session，bg task 安全）。"""
import random
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.config import settings
from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.rewrite_event import RewriteEvent


async def log(strategy, original, rewritten, improved, orig_score, new_score, cached, route="hybrid", tenant="default"):
    """采样写一条事件。bg task 调用，独立 session。"""
    if random.random() > settings.REWRITE_EVENT_SAMPLE_RATE:
        return
    try:
        async with AsyncSessionLocal() as db:
            db.add(RewriteEvent(
                strategy=strategy, original_query=original[:500], rewritten_query=(rewritten or "")[:500],
                improved=1 if improved else 0, orig_score=float(orig_score or 0), new_score=float(new_score or 0),
                cached=1 if cached else 0, route=route, tenant=tenant,
            ))
            await db.commit()
    except Exception as e:
        degraded("rewrite_event_log", e)


def _period_start(period: str) -> datetime:
    n = datetime.now()
    return n - (timedelta(days=1) if period == "today" else timedelta(days=7))


async def stats(period: str = "today") -> dict:
    try:
        async with AsyncSessionLocal() as db:
            start = _period_start(period)
            base = select(func.count()).select_from(RewriteEvent).where(RewriteEvent.ts >= start)
            total = (await db.execute(base)).scalar() or 0
            adopted = (await db.execute(base.where(RewriteEvent.improved == 1))).scalar() or 0
            cached = (await db.execute(base.where(RewriteEvent.cached == 1))).scalar() or 0
            # byStrategy
            rows = (await db.execute(
                select(RewriteEvent.strategy, RewriteEvent.improved, func.count())
                .where(RewriteEvent.ts >= start)
                .group_by(RewriteEvent.strategy, RewriteEvent.improved)
            )).all()
            by_strategy = {}
            for strat, imp, cnt in rows:
                d = by_strategy.setdefault(strat, {"count": 0, "adopted": 0})
                d["count"] += cnt
                if imp:
                    d["adopted"] += cnt
            return {
                "total": total, "adopted": adopted, "rejected": total - adopted,
                "cacheHit": cached,
                "adoptedRate": round(adopted / total, 3) if total else 0,
                "cacheHitRate": round(cached / total, 3) if total else 0,
                "byStrategy": by_strategy,
            }
    except Exception as e:
        degraded("rewrite_event_stats", e)
        return {"total": 0, "adopted": 0, "rejected": 0, "cacheHit": 0, "byStrategy": {}}


async def events_page(page: int = 1, size: int = 20, strategy: str | None = None, adopted: bool | None = None) -> dict:
    try:
        async with AsyncSessionLocal() as db:
            q = select(RewriteEvent).order_by(RewriteEvent.ts.desc())
            cq = select(func.count()).select_from(RewriteEvent)
            if strategy:
                q = q.where(RewriteEvent.strategy == strategy); cq = cq.where(RewriteEvent.strategy == strategy)
            if adopted is not None:
                q = q.where(RewriteEvent.improved == (1 if adopted else 0))
                cq = cq.where(RewriteEvent.improved == (1 if adopted else 0))
            total = (await db.execute(cq)).scalar() or 0
            rows = (await db.execute(q.offset((page - 1) * size).limit(size))).scalars().all()
            return {"total": total, "list": [{
                "ts": r.ts.strftime("%Y-%m-%d %H:%M:%S") if r.ts else "",
                "strategy": r.strategy, "original": r.original_query, "rewritten": r.rewritten_query,
                "improved": bool(r.improved), "origScore": r.orig_score, "newScore": r.new_score,
                "cached": bool(r.cached),
            } for r in rows]}
    except Exception as e:
        degraded("rewrite_event_page", e)
        return {"total": 0, "list": []}
```

- [ ] **Step 5: 运行迁移 + 测试 + commit**

```bash
docker exec grid-backend alembic upgrade head
pytest tests/test_rewrite_event.py -v
git add backend/app/models/rewrite_event.py backend/migrations/versions/ backend/app/services/rewrite_event_service.py tests/test_rewrite_event.py
git commit -m "feat(rewrite): rewrite_event 表 + EventLogger（采样记录+聚合查询）"
```

---

## Task 6: query_rewrite.py 改造

**Files:**
- Modify: `backend/app/services/query_rewrite.py`
- Test: `tests/test_rewrite_integration.py`（集成：Classifier+Cache+Evaluator 协同）

**Interfaces:**
- Consumes: Task 2/3/4 的 Classifier/Cache/Evaluator；`get_fewshot`；EventLogger.log
- Produces: `async rewrite_query_v2(query, model_type, strategy_hint=None) -> dict` 返回 `{"query": str, "strategy": str, "improved": bool, "cached": bool, "orig_score": float, "new_score": float}`；保留旧 `rewrite_query(force=True)` 供 CRAG（绕过 adaptive/eval）

- [ ] **Step 1: 写集成失败测试** `tests/test_rewrite_integration.py`

```python
import pytest
from unittest.mock import AsyncMock, patch
from app.services import query_rewrite

@pytest.mark.asyncio
async def test_normal_query_skipped():
    """规范 query 被 Classifier 判 normal → 直接返回原 query，不调 LLM。"""
    with patch.object(query_rewrite, "get_llm_provider") as mk_llm:
        r = await query_rewrite.rewrite_query_v2("主变压器绕组温度过热的应急处置步骤", None)
        assert r["query"] == "主变压器绕组温度过热的应急处置步骤"
        assert r["strategy"] == "normal"
        mk_llm.assert_not_called()  # 没调 LLM

@pytest.mark.asyncio
async def test_cache_hit_skips_llm():
    with patch.object(query_rewrite.rewrite_cache, "get", AsyncMock(return_value={"result": "改写后", "improved": True})):
        with patch.object(query_rewrite, "get_llm_provider") as mk_llm:
            r = await query_rewrite.rewrite_query_v2("咋办", None)
            assert r["cached"] is True
            assert r["query"] == "改写后"
            mk_llm.assert_not_called()
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/test_rewrite_integration.py -v
```
Expected: FAIL（`rewrite_query_v2` 不存在）

- [ ] **Step 3: 改造 query_rewrite.py**

在 `backend/app/services/query_rewrite.py` 保留旧 `rewrite_query`（CRAG force 用），追加：

```python
from app.services import rewrite_cache, rewrite_evaluator
from app.services.rewrite_strategy import classify, get_fewshot
from app.services import rewrite_event_service


def _build_prompt(query: str, strategy: dict) -> str:
    fs = get_fewshot(strategy["type"])
    examples = "".join(f"示例：{e['q']} → {e['r']}\n" for e in fs)
    return (
        "你是电网运维检索查询改写助手。把下面提问改写为更规范、信息更完整、适合向量检索的查询"
        f"（{strategy['hint']}，保留关键设备/故障/操作术语，去掉口语）。只输出改写后查询，不要解释：\n"
        f"{examples}输入：{query}\n输出："
    )


async def rewrite_query_v2(query: str, model_type: str | None = None) -> dict:
    """完整改写：Classifier→Cache→改写→Evaluator→记事件。规范 query 跳过（adaptive）。"""
    strategy = classify(query)
    if strategy["skip"] and settings.REWRITE_ADAPTIVE_ENABLE:
        return {"query": query, "strategy": "normal", "improved": False, "cached": False,
                "orig_score": 0.0, "new_score": 0.0}
    # 缓存
    cached = await rewrite_cache.get(strategy["type"], query)
    if cached:
        try:
            from app.core import metrics
            metrics.REWRITE_CACHE_HIT.labels("rewrite").inc()
        except Exception:
            pass
        await rewrite_event_service.log("rewrite", query, cached.get("result", query),
                                        cached.get("improved", False), 0, 0, cached=True)
        return {"query": cached.get("result", query) if cached.get("improved") else query,
                "strategy": strategy["type"], "improved": cached.get("improved", False),
                "cached": True, "orig_score": 0.0, "new_score": 0.0}
    # LLM 改写（带 few-shot）
    rewritten = query
    try:
        rewritten = (await get_llm_provider(model_type).chat(
            [{"role": "user", "content": _build_prompt(query, strategy)}],
            temperature=0, max_tokens=120,
        )).strip() or query
    except Exception as e:
        degraded("query_rewrite_v2", e)
        rewritten = query
    # 评估
    improved, orig_s, new_s = False, 0.0, 0.0
    if settings.REWRITE_EVAL_ENABLE and rewritten != query:
        ev = await rewrite_evaluator.evaluate(query, rewritten, model_type)
        improved, orig_s, new_s = ev["improved"], ev["orig_score"], ev["new_score"]
    result = rewritten if improved else query
    # 写缓存 + 记事件 + 指标
    await rewrite_cache.set(strategy["type"], query, {"result": rewritten, "improved": improved})
    await rewrite_event_service.log("rewrite", query, rewritten, improved, orig_s, new_s, cached=False)
    try:
        from app.core import metrics
        (metrics.REWRITE_IMPROVED if improved else metrics.REWRITE_EVAL_REJECTED).labels("rewrite").inc()
    except Exception:
        pass
    return {"query": result, "strategy": strategy["type"], "improved": improved,
            "cached": False, "orig_score": orig_s, "new_score": new_s}
```

- [ ] **Step 4: 运行通过 + commit**

```bash
pytest tests/test_rewrite_integration.py -v
git add backend/app/services/query_rewrite.py tests/test_rewrite_integration.py
git commit -m "feat(rewrite): query_rewrite_v2 接入 Classifier+Cache+Evaluator+Event（保留 force 旧路径）"
```

---

## Task 7: mixed_search 接入 + multi/hyde 缓存

**Files:**
- Modify: `backend/app/services/retrieval_service.py`（`mixed_search` step 0 + multi_query/hyde 加 Cache）
- Test: `tests/test_rewrite_integration.py` 追加 e2e

**Interfaces:**
- Consumes: Task 6 `rewrite_query_v2`；Task 3 `rewrite_cache`（multi/hyde）
- Produces: `mixed_search` 内部改造（外部签名不变）

- [ ] **Step 1: 改 mixed_search step 0**

`backend/app/services/retrieval_service.py` 中 `mixed_search` 的 step 0：

```python
    # 0) query 改写（口语→规范，含 adaptive 跳过 + 缓存 + 评估）
    from app.services import query_rewrite as _qr
    _rw = await _qr.rewrite_query_v2(query, model_type)
    q = _rw["query"]
    _rw_route = route  # 留给事件用
```

（替换原 `q = await query_rewrite.rewrite_query(query, model_type)`）

- [ ] **Step 2: multi_query 加缓存**

`mixed_search` 的 multi_query 块（step 0.5）：

```python
    if route != "sparse" and getattr(settings, "MULTI_QUERY_ENABLE", False):
        from app.services import multi_query, rewrite_cache
        try:
            cached_mq = await rewrite_cache.get("multi", query)
            if cached_mq:
                subs = cached_mq.get("subs", [])
            else:
                subs = await multi_query.decompose(query, model_type) or []
                await rewrite_cache.set("multi", query, {"subs": subs})
            if subs:
                queries.extend(subs)
        except Exception as e:
            degraded("multi_query_dispatch", e)
```

- [ ] **Step 3: hyde 加缓存**

`_dense_and_sparse` 与 dense 分支的 HyDE 块，把 `ht = await hyde.generate_hypothetical(qq, model_type)` 改为缓存版：

```python
            ht = None
            cached_hyde = await rewrite_cache.get("hyde", qq)
            if cached_hyde:
                ht = cached_hyde.get("hypothetical")
            else:
                ht = await hyde.generate_hypothetical(qq, model_type)
                if ht:
                    await rewrite_cache.set("hyde", qq, {"hypothetical": ht})
```

（dense 分支与 `_dense_and_sparse` 两处同样替换）

- [ ] **Step 4: 语法 + 集成测试 + commit**

```bash
python -m py_compile backend/app/services/retrieval_service.py
pytest tests/test_rewrite_integration.py -v
git add backend/app/services/retrieval_service.py
git commit -m "feat(rewrite): mixed_search 接入 rewrite_query_v2 + multi/hyde 缓存"
```

---

## Task 8: system router 两聚合接口

**Files:**
- Modify: `backend/app/routers/system.py`（tune-cache 路由后）
- Test: `tests/test_rewrite_api.py`

**Interfaces:**
- Consumes: Task 5 `rewrite_event_service.stats` / `events_page`；`require_admin`
- Produces: `GET /system/optimizer/rewrite-stats` / `GET /system/optimizer/rewrite-events`

- [ ] **Step 1: 写失败测试** `tests/test_rewrite_api.py`

```python
import pytest
from app.test_client import async_client  # 项目已有测试 client fixture

@pytest.mark.asyncio
async def test_rewrite_stats_requires_admin(async_client):
    r = await async_client.get("/api/system/optimizer/rewrite-stats")
    assert r.status_code == 401

@pytest.mark.asyncio
async def test_rewrite_stats_ok(async_client, admin_token):
    r = await async_client.get("/api/system/optimizer/rewrite-stats",
                               headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert "total" in r.json()["data"]
```

- [ ] **Step 2: 加路由** `backend/app/routers/system.py`（tune-cache 后）

```python
@router.get("/optimizer/rewrite-stats")
async def optimizer_rewrite_stats(
    period: str = "today",
    admin: User = Depends(require_admin),
):
    """Query 改写质量评估统计（总数/采纳率/缓存命中/策略分布）。"""
    from app.services.rewrite_event_service import stats
    return success(await stats(period), "查询成功")


@router.get("/optimizer/rewrite-events")
async def optimizer_rewrite_events(
    page: int = 1, size: int = 20, strategy: str | None = None,
    adopted: bool | None = None,
    admin: User = Depends(require_admin),
):
    """Query 改写事件明细（可按策略/采纳过滤）。"""
    from app.services.rewrite_event_service import events_page
    return success(await events_page(page, size, strategy, adopted), "查询成功")
```

- [ ] **Step 3: 运行通过 + commit**

```bash
pytest tests/test_rewrite_api.py -v
git add backend/app/routers/system.py tests/test_rewrite_api.py
git commit -m "feat(rewrite): rewrite-stats / rewrite-events 两聚合接口（admin）"
```

---

## Task 9: 前端「🔧 Query改写」可视化面板

**Files:**
- Modify: `frontend/src/views/Admin.vue`（新增 tab + 面板 script/template/style）

**Interfaces:**
- Consumes: Task 8 两接口（`request.get('/system/optimizer/rewrite-stats')` / `rewrite-events`）
- Produces: Admin 新 tab「🔧 Query改写」，含概览卡 / 策略饼图 / 趋势线 / 分数散点 / 明细表

> 前端无单测，验收靠 Vite 编译 + 手测。复用项目已有 echarts（`echarts.use([PieChart, BarChart, LineChart, ScatterChart, ...])` 已在 Admin.vue 配置）。

- [ ] **Step 1: template 加 tab 按钮**

在 Admin.vue 顶部 tab 栏「📈 优化建议」按钮后追加：

```vue
<button class="tab" :class="{ active: tab === 'rewrite' }" @click="loadRewrite(); tab = 'rewrite'">🔧 Query改写</button>
```

- [ ] **Step 2: template 加面板**

在「优化建议」card 的 `</div>` 后追加新 card：

```vue
<!-- Query 改写质量评估 -->
<div class="card" v-show="tab === 'rewrite'">
  <div class="card-header">
    <h3 class="card-title">🔧 Query 改写质量评估</h3>
    <select v-model="rwPeriod" @change="loadRewrite" class="btn btn-ghost btn-sm">
      <option value="today">今天</option><option value="7d">近7天</option>
    </select>
  </div>
  <div v-if="rwStats" class="rw-overview">
    <div class="stat">总改写 <b>{{ rwStats.total }}</b></div>
    <div class="stat">采纳率 <b>{{ (rwStats.adoptedRate*100).toFixed(0) }}%</b></div>
    <div class="stat">否决率 <b>{{ ((1-rwStats.adoptedRate)*100).toFixed(0) }}%</b></div>
    <div class="stat">缓存命中 <b>{{ (rwStats.cacheHitRate*100).toFixed(0) }}%</b></div>
  </div>
  <div style="display:flex; gap:12px; flex-wrap:wrap; margin:12px 0">
    <div ref="rwPieEl" style="width:48%; height:260px"></div>
    <div ref="rwScatterEl" style="width:48%; height:260px"></div>
  </div>
  <div class="card-header"><h4>改写明细</h4>
    <button class="btn btn-ghost btn-sm" @click="loadRewriteEvents">🔄 刷新</button>
  </div>
  <table class="tbl" v-if="rwEvents.length">
    <thead><tr><th>时间</th><th>策略</th><th>原 query</th><th>改写</th><th>采纳</th><th>分数(原→新)</th></tr></thead>
    <tbody>
      <tr v-for="(e,i) in rwEvents" :key="i">
        <td>{{ e.ts }}</td><td>{{ e.strategy }}</td>
        <td>{{ (e.original||'').slice(0,30) }}</td><td>{{ (e.rewritten||'').slice(0,30) }}</td>
        <td><span :class="e.improved ? 'badge badge-success' : 'badge badge-neutral'">{{ e.improved ? '✓' : '✗' }}</span></td>
        <td>{{ e.origScore?.toFixed(2) }} → {{ e.newScore?.toFixed(2) }}</td>
      </tr>
    </tbody>
  </table>
  <div v-else class="empty">暂无改写事件（先在 Chat 问几个口语化问题积累数据）</div>
</div>
```

- [ ] **Step 3: script 加数据 + 渲染函数**

在 Admin.vue script（`loadOptimizer` 附近）追加：

```javascript
const rwStats = ref(null); const rwEvents = ref([]); const rwPeriod = ref('today')
const rwPieEl = ref(null); const rwScatterEl = ref(null)
async function loadRewrite() {
  try {
    rwStats.value = (await request.get('/system/optimizer/rewrite-stats', { params: { period: rwPeriod.value } })).data
    loadRewriteEvents(); renderRwCharts()
  } catch (e) { toast('加载失败') }
}
async function loadRewriteEvents() {
  try { rwEvents.value = (await request.get('/system/optimizer/rewrite-events', { params: { size: 50 } })).data.list || [] }
  catch (e) { rwEvents.value = [] }
}
function renderRwCharts() {
  if (!rwStats.value) return
  // 策略分布饼图
  const pie = echarts.init(rwPieEl.value)
  const bs = rwStats.value.byStrategy || {}
  pie.setOption({ title: { text: '策略分布', left: 'center', textStyle: { fontSize: 13 } },
    series: [{ type: 'pie', radius: ['40%','70%'], data: Object.entries(bs).map(([k,v]) => ({ name: k, value: v.count })) }] })
  // 改写前后分数散点
  const sc = echarts.init(rwScatterEl.value)
  const ev = rwEvents.value
  sc.setOption({ title: { text: '改写前后分数（对角线上方=改进）', left: 'center', textStyle: { fontSize: 13 } },
    xAxis: { name: '原分数', type: 'value' }, yAxis: { name: '新分数', type: 'value' },
    series: [{ type: 'scatter', symbolSize: 6,
      data: ev.map(e => [e.origScore, e.newScore]),
      itemStyle: { color: '#3b82f6' } }] })
}
```

- [ ] **Step 4: 编译验证 + commit**

```bash
curl -s http://localhost:5173/src/views/Admin.vue | grep -c "loadRewrite"  # 应 >0，无编译错误
git add frontend/src/views/Admin.vue
git commit -m "feat(frontend): Admin「🔧 Query改写」面板（概览+策略饼图+分数散点+明细）"
```

---

## Task 10: 端到端验证 + golden 对比

**Files:**
- 无新文件，验证 Task 1-9 集成

- [ ] **Step 1: rebuild backend**

```bash
docker compose up -d --build backend
sleep 12
curl -s http://localhost:8001/health   # 期望 healthy
```

- [ ] **Step 2: 触发改写 + 看事件**

```bash
# 问一个口语化问题（触发 colloquial 改写）
TOK=$(curl -s -X POST http://localhost:8001/api/system/login -H "Content-Type: application/json" -d '{"username":"admin","password":"admin123"}' | python -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
curl -s -X POST http://localhost:8001/api/qa/answer -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" -d '{"query":"主变烧了咋办","modelType":"deepseek"}'
sleep 2
# 看事件是否记录
curl -s "http://localhost:8001/api/system/optimizer/rewrite-stats?period=today" -H "Authorization: Bearer $TOK"
# 期望 total>=1, adoptedRate 有值
```

- [ ] **Step 3: 验证缓存命中（二次问同问题）**

```bash
# 再问同一个口语化问题 → 缓存命中，cacheHitRate 上升
curl -s -X POST http://localhost:8001/api/qa/answer -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" -d '{"query":"主变烧了咋办","modelType":"deepseek"}'
sleep 2
curl -s "http://localhost:8001/api/system/optimizer/rewrite-stats?period=today" -H "Authorization: Bearer $TOK"
# 期望 cacheHit >= 1
```

- [ ] **Step 4: golden 不退化对比**

用 `scripts/eval_retrieval.py`（项目已有）跑 golden 集，对比改写升级前后的 recall：

```bash
docker exec grid-backend python /app/scripts/eval_retrieval.py 2>&1 | tail -20
# 期望 recall 不降（adaptive 跳过规范 query + 评估否决坏改写，应持平或提升）
```

- [ ] **Step 5: 前端手测 + 最终 commit**

浏览器 `localhost:5173` → Admin → 「🔧 Query改写」tab，确认概览/饼图/散点/明细渲染。

```bash
git log --oneline -10   # 确认 Task 1-9 各一个 commit
```

---

## Self-Review

**1. Spec 覆盖**：
- §4.1 Classifier → Task 2 ✓
- §4.2 Cache → Task 3 ✓
- §4.3 Evaluator → Task 4 ✓
- §4.4 EventLogger → Task 5 ✓
- §5 数据流（mixed_search）→ Task 7 ✓
- §6 错误处理（degraded）→ 各任务实现含 try/except + degraded ✓
- §7 测试 → 每任务 TDD ✓
- §8 配置 → Task 1 ✓
- §9 文件 → 全覆盖 ✓
- §11 验收 → Task 10 验证 ✓
- §12 前端可视化 → Task 9 ✓

**2. Placeholder 扫描**：无 TBD/TODO；每步含具体代码/命令。Alembic 迁移文件名用 `<ts>_add_rewrite_event.py`（生成时自动命名，Step 2 已说明 autogenerate）。

**3. 类型一致**：
- `rewrite_query_v2` 返回 `{"query","strategy","improved","cached","orig_score","new_score"}` —— Task 6/7/9 引用一致 ✓
- `stats()` 返回 `{total,adopted,rejected,cacheHit,adoptedRate,cacheHitRate,byStrategy}` —— Task 8/9 一致 ✓
- `events_page()` 返回 `{total,list:[{ts,strategy,original,rewritten,improved,origScore,newScore,cached}]}` —— Task 8/9 一致 ✓
- `classify()` 返回 `{type,skip,hint}` —— Task 2/6 一致 ✓
