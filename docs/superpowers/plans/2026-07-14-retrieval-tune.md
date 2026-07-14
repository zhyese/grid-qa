# 检索质量自动调参建议（只建议模式）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans（本机 glm-5 subagent 不可用，禁用 subagent-driven）。Steps use checkbox (`- [ ]`).

**Goal:** 把检索评测/调优三座孤岛打通成"评测→扫描→建议"闭环（只建议，人工改 `.env`）。

**Architecture:** `mixed_search` 加 `overrides` 可选参数（13 caller 零破坏）→ `retrieval_eval_service` 直接调它跑 golden 算 recall/MRR/nDCG → 扫描引擎扰动+开关 A/B → 报告缓存 + Admin tab + 「复制 .env 行」。

**Tech Stack:** FastAPI + Vue3(echarts) + golden_qa.json（无新外部依赖）。

## Global Constraints

- 后端从项目根跑：`venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend --port 8001`
- 测试：`PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/xxx.py -v`
- 只建议模式：系统不写 `settings`，不动 `.env`，不改运行时参数
- `mixed_search` 现有 13 caller 必须零破坏（`overrides` 默认 None）
- golden 12 条小样本 → 扰动扫描（非网格/坐标下降），四道护栏防过拟合

---

## File Structure

**新建：**
- `backend/app/services/retrieval_eval_service.py` — recall/MRR/nDCG 纯函数 + `evaluate_over_golden`
- `backend/app/services/retrieval_tune_service.py` — 扫描引擎 + 参数空间 + 建议规则 + 报告缓存
- `backend/app/routers/retrieval_tune_router.py` — `/system/retrieval/tune(+report)`
- `backend/app/schemas/retrieval_tune.py` — 报告 schema
- `tests/test_retrieval_eval_metrics.py` / `tests/test_retrieval_tune_engine.py` / `tests/test_mixed_search_overrides.py` / `tests/test_retrieval_tune_api.py`

**修改：**
- `backend/app/services/retrieval_service.py` — `mixed_search` 加 `overrides` 参数 + `_ov` helper
- `backend/app/config.py` — 加 `TUNE_*` 字段
- `.env.example` — 加 `TUNE_*` 对齐
- `backend/app/core/metrics.py` — 加 `RETRIEVAL_TUNE_TOTAL` / `RETRIEVAL_BASELINE` + 预注册
- `backend/app/main.py` — 挂 retrieval_tune_router
- `frontend/src/views/Admin.vue` — 加「检索调参」tab
- `frontend/src/api/index.js` — 加 retrievalTune API

---

### Task 1: 配置字段 + mixed_search overrides

**Files:**
- Modify: `backend/app/config.py`（CRAG_TIMEOUT 后加 TUNE_*）
- Modify: `.env.example`
- Modify: `backend/app/services/retrieval_service.py`（`mixed_search` 加 overrides + `_ov`）
- Test: `tests/test_mixed_search_overrides.py`

**Interfaces:**
- Produces: `settings.TUNE_ENABLE/MIN_IMPROVE/MIN_SAMPLE/SCAN_TOPK`；`mixed_search(..., overrides=None)`；`_ov(ov, key, default)`

- [ ] **Step 1: config.py 加字段**

CRAG_TIMEOUT 行后加：
```python

    # ---------- 检索参数调优（只建议模式）----------
    TUNE_ENABLE: bool = True
    TUNE_MIN_IMPROVE: float = 0.02      # 出建议的最小提升（防噪声）
    TUNE_MIN_SAMPLE: int = 10           # 最小有效样本（防小样本过拟合）
    TUNE_SCAN_TOPK: int = 5             # 扫描评测用 topk
```
`.env.example` 对齐加这 4 行。

- [ ] **Step 2: 写失败测试**

`tests/test_mixed_search_overrides.py`：
```python
import pytest

@pytest.mark.asyncio
async def test_overrides_applied(monkeypatch):
    """传 overrides 时，mixed_search 内部用到 override 值。"""
    from app.services import retrieval_service
    from app.config import settings
    captured = {}
    async def _fake_mixed(db, query, topk, **kw):  # 简化：只验参数读取
        return []
    # 直接测 _ov helper
    assert retrieval_service._ov({"RRF_K": 40}, "RRF_K", 60) == 40
    assert retrieval_service._ov(None, "RRF_K", 60) == 60
    assert retrieval_service._ov({}, "RRF_K", 60) == 60

@pytest.mark.asyncio
async def test_mixed_search_default_no_overrides(monkeypatch):
    """overrides=None 时读 settings（默认路径，保护 13 caller）。"""
    from app.services import retrieval_service
    # mock milvus/bm25 为空，确保不报错且走到 settings 读取
    monkeypatch.setattr(retrieval_service.milvus_client, "search", lambda *a, **k: [])
    monkeypatch.setattr(retrieval_service.bm25_service, "search", lambda *a, **k: [])
    monkeypatch.setattr(retrieval_service.bm25_service, "ensure_built", lambda *a, **k: None)
    monkeypatch.setattr(retrieval_service.bm25_service, "get_chunk", lambda *a, **k: None)
    from app.db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        result = await retrieval_service.mixed_search(db, "测试", 5, overrides=None)
    assert isinstance(result, list)  # 不报错即默认路径正常
```

- [ ] **Step 3: 跑测试验证失败**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_mixed_search_overrides.py -v`
Expected: FAIL（`_ov` 不存在）

- [ ] **Step 4: 改 retrieval_service.py**

文件顶部加 helper：
```python
def _ov(ov: dict | None, key: str, default):
    """overrides 读取：ov 非空且含 key 取 ov[key]，否则 default。"""
    return ov.get(key, default) if ov else default
```
`mixed_search` 签名加 `overrides: dict | None = None`，函数体内把读 settings 的检索参数改为 `_ov`：
```python
    # 原 settings.RRF_K →
    rrf_k = _ov(overrides, "RRF_K", settings.RRF_K)
    # 原 settings.RRF_DENSE_WEIGHT / RRF_SPARSE_WEIGHT →
    dense_w = _ov(overrides, "RRF_DENSE_WEIGHT", settings.RRF_DENSE_WEIGHT)
    sparse_w = _ov(overrides, "RRF_SPARSE_WEIGHT", settings.RRF_SPARSE_WEIGHT)
    # 原 settings.MMR_LAMBDA →
    mmr_lambda = _ov(overrides, "MMR_LAMBDA", settings.MMR_LAMBDA)
    # 原 settings.RERANK_ENABLE / MMR_ENABLE →
    rerank_on = _ov(overrides, "RERANK_ENABLE", settings.RERANK_ENABLE)
    mmr_on = _ov(overrides, "MMR_ENABLE", settings.MMR_ENABLE)
    # topk 可被 overrides["TOPK"] 覆盖
    topk = _ov(overrides, "TOPK", topk)
```
（CRAG_HIGH/LOW 在 qa_service 的 `_crag_correct`，Task 3 扫描若含 CRAG 需同样改 `_crag_correct` 接 overrides 透传——见 Task 3 注）

- [ ] **Step 5: 跑测试 + 回归验证通过**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_mixed_search_overrides.py tests/test_rewrite_strategy.py -v`
Expected: PASS（_ov 测试过 + 现有 retrieval 相关回归不破坏）

- [ ] **Step 6: Commit**

```bash
git add backend/app/config.py .env.example backend/app/services/retrieval_service.py tests/test_mixed_search_overrides.py
git commit -m "feat(tune): mixed_search overrides 参数（13 caller 零破坏）+ TUNE_* 配置"
```

---

### Task 2: retrieval_eval_service（评测纯函数 + golden 评测）

**Files:**
- Create: `backend/app/services/retrieval_eval_service.py`
- Test: `tests/test_retrieval_eval_metrics.py`

**Interfaces:**
- Produces: `_recall_at_k(expect, got)` / `_mrr(expect, got)` / `_ndcg(relevant_docs, got)` / `evaluate_over_golden(db, overrides, topk)`

- [ ] **Step 1: 写失败测试**

`tests/test_retrieval_eval_metrics.py`：
```python
from app.services.retrieval_eval_service import _recall_at_k, _mrr, _ndcg

def test_recall_at_k():
    expect = ["主变运维手册.pdf", "故障案例.docx"]
    got = ["其他.doc", "主变运维手册.pdf", "故障案例.docx"]
    assert _recall_at_k(expect, got) == 1.0   # 两个都命中

def test_recall_partial():
    assert _recall_at_k(["A", "B"], ["A", "C"]) == 0.5

def test_mrr():
    expect = ["B"]
    assert _mrr(expect, ["A", "B", "C"]) == 1/2  # 排第2
    assert _mrr(expect, ["B"]) == 1.0

def test_ndcg_graded():
    # relevant_docs: {"主变运维手册.pdf": 3, "其他.doc": 1}
    rd = {"主变运维手册.pdf": 3, "其他.doc": 1}
    got = ["主变运维手册.pdf", "其他.doc"]
    score = _ndcg(rd, got)
    assert 0 < score <= 1.0
```

- [ ] **Step 2: 跑测试验证失败**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_retrieval_eval_metrics.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 写实现**

`backend/app/services/retrieval_eval_service.py`：
```python
"""检索评测 service：服务化 eval_retrieval，直接调 mixed_search，算 recall/MRR/nDCG。"""
import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import retrieval_service

_GOLDEN = Path(__file__).resolve().parent.parent.parent / "data" / "golden_qa.json"


def _load_golden() -> list[dict]:
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


def _recall_at_k(expect: list[str], got: list[str]) -> float:
    if not expect:
        return 0.0
    hit = sum(1 for d in expect if d in got)
    return hit / len(expect)


def _mrr(expect: list[str], got: list[str]) -> float:
    for i, d in enumerate(got, 1):
        if d in expect:
            return 1.0 / i
    return 0.0


def _ndcg(relevant_docs: dict, got: list[str]) -> float:
    """分级 nDCG（relevant_docs value 1-3 为相关性等级）。"""
    def _dcg(order):
        s = 0.0
        for i, d in enumerate(order, 1):
            rel = relevant_docs.get(d, 0)
            s += (2 ** rel - 1) / (i + 1) if rel else 0
        return s
    ideal = sorted(relevant_docs.values(), reverse=True)
    idcg = sum((2 ** r - 1) / (i + 1) for i, r in enumerate(ideal, 1))
    if idcg == 0:
        return 0.0
    return _dcg(got) / idcg


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 4) if xs else 0.0


async def evaluate_over_golden(db: AsyncSession, overrides: dict | None = None, topk: int = 5) -> dict:
    golden = _load_golden()
    recalls, mrrs, ndcgs, n_empty = [], [], [], 0
    per_query = []
    for item in golden:
        ctx = await retrieval_service.mixed_search(db, item["query"], topk, overrides=overrides)
        got = [c["docName"] for c in ctx] if ctx else []
        if not ctx:
            n_empty += 1
            per_query.append({"query": item["query"], "recall": 0, "mrr": 0, "empty": True})
            continue
        r = _recall_at_k(item.get("expect", []), got)
        m = _mrr(item.get("expect", []), got)
        recalls.append(r); mrrs.append(m)
        n = _ndcg(item.get("relevant_docs", {}), got) if item.get("relevant_docs") else None
        if n is not None:
            ndcgs.append(n)
        per_query.append({"query": item["query"], "recall": r, "mrr": m, "ndcg": n})
    return {
        "recall": _mean(recalls), "mrr": _mean(mrrs), "ndcg": _mean(ndcgs),
        "noResultRate": round(n_empty / len(golden), 4), "sampleSize": len(golden),
        "validSample": len(recalls), "perQuery": per_query,
    }
```

- [ ] **Step 4: 跑测试验证通过**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_retrieval_eval_metrics.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retrieval_eval_service.py tests/test_retrieval_eval_metrics.py
git commit -m "feat(tune): 评测 service（recall/MRR/nDCG 纯函数 + golden 评测）"
```

---

### Task 3: 扫描引擎 + 建议规则 + 报告缓存

**Files:**
- Create: `backend/app/services/retrieval_tune_service.py`
- Test: `tests/test_retrieval_tune_engine.py`

**Interfaces:**
- Produces: `PARAM_SPACE` / `SWITCHES` / `run_scan(db)` / `generate_tune_report(db)` / `get_tune_report()`

- [ ] **Step 1: 写失败测试**

`tests/test_retrieval_tune_engine.py`：
```python
import pytest

@pytest.mark.asyncio
async def test_build_suggestions_filters_below_margin(monkeypatch):
    """提升 < margin 不出建议。"""
    from app.services import retrieval_tune_service as ts
    from app.config import settings
    baseline = {"recall": 0.90, "mrr": 0.85, "ndcg": 0.88, "noResultRate": 0.0, "validSample": 12}
    # RRF_K=40 提升 0.005（< 0.02 margin）→ 不出；RRF_K=80 提升 0.03 → 出
    scan = [
        {"param": "RRF_K", "value": 40, "recall": 0.905, "mrr": 0.85, "ndcg": 0.88},
        {"param": "RRF_K", "value": 80, "recall": 0.93, "mrr": 0.86, "ndcg": 0.89},
    ]
    suggestions = ts._build_suggestions(baseline, scan, settings.TUNE_MIN_IMPROVE)
    params = [s["param"] for s in suggestions]
    assert all(s["param"] == "RRF_K" and s["suggested"] == 80 for s in suggestions)
    assert len(suggestions) == 1

def test_param_space_covers_key_params():
    from app.services.retrieval_tune_service import PARAM_SPACE
    keys = set(PARAM_SPACE.keys())
    assert {"RRF_K", "MMR_LAMBDA", "RRF_DENSE_WEIGHT"}.issubset(keys)
```

- [ ] **Step 2: 跑测试验证失败**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_retrieval_tune_engine.py -v`
Expected: FAIL

- [ ] **Step 3: 写实现**

`backend/app/services/retrieval_tune_service.py`：
```python
"""检索参数扫描引擎（只建议模式）：扰动 + 开关 A/B → 报告。"""
import asyncio
import time
from pathlib import Path

from app.config import settings
from app.core.obs import degraded
from app.services import retrieval_eval_service

_REPORT = Path(__file__).resolve().parent.parent.parent / "data" / "tune_report.json"

# 连续参数候选值（除当前值；当前值在扫描时读 settings 填充）
PARAM_SPACE = {
    "RRF_K": [40, 80],
    "MMR_LAMBDA": [0.3, 0.7],
    "RRF_DENSE_WEIGHT": [0.7, 1.3],
    "TOPK": [3, 8],
    "CRAG_HIGH": [round(settings.CRAG_HIGH - 0.1, 2), round(settings.CRAG_HIGH + 0.1, 2)],
    "CRAG_LOW": [round(settings.CRAG_LOW - 0.1, 2), round(settings.CRAG_LOW + 0.1, 2)],
}
SWITCHES = ["RERANK_ENABLE", "HYDE_ENABLE", "MULTI_QUERY_ENABLE", "SMALL_TO_BIG_ENABLE"]

# RRF_DENSE_WEIGHT 改变需同步 SPARSE 权重（保持归一化参考），单独处理
_PAIRED = {"RRF_DENSE_WEIGHT": "RRF_SPARSE_WEIGHT"}


def _current(param):
    return getattr(settings, param)


def _build_suggestions(baseline: dict, scan: list[dict], min_improve: float) -> list[dict]:
    """对比 baseline，按四道护栏产出建议。"""
    by_param: dict[str, list[dict]] = {}
    for row in scan:
        by_param.setdefault(row["param"], []).append(row)
    suggestions = []
    for param, rows in by_param.items():
        best = max(rows, key=lambda r: r["recall"] - baseline["recall"])
        d_recall = best["recall"] - baseline["recall"]
        d_mrr = best["mrr"] - baseline["mrr"]
        if d_recall < min_improve:
            continue
        # 多指标同向判定 confidence
        if d_recall >= 0.05 and d_mrr >= 0:
            conf = "high"
        elif d_mrr >= 0:
            conf = "medium"
        else:
            conf = "low"
        suggestions.append({
            "param": param, "current": _current(param), "suggested": best["value"],
            "metric": "recall", "delta": round(d_recall, 4), "confidence": conf,
            "reason": f"recall {baseline['recall']:.3f}→{best['recall']:.3f}, MRR {baseline['mrr']:.3f}→{best['mrr']:.3f}",
        })
    return suggestions


async def run_scan(db) -> dict:
    """跑完整扫描，写报告，返回报告 dict。"""
    if not settings.TUNE_ENABLE:
        return get_tune_report()
    t0 = time.time()
    topk = settings.TUNE_SCAN_TOPK
    try:
        baseline = await retrieval_eval_service.evaluate_over_golden(db, overrides=None, topk=topk)
    except Exception as e:
        degraded("tune_baseline", e)
        return {"error": f"baseline 评测失败: {e}"}

    if baseline["validSample"] < settings.TUNE_MIN_SAMPLE:
        return {"error": f"有效样本不足({baseline['validSample']}<{settings.TUNE_MIN_SAMPLE})，扫描中止"}

    scan_matrix, switches_result = [], []
    try:
        # 连续参数扰动
        for param, candidates in PARAM_SPACE.items():
            cur = _current(param)
            for val in [cur] + candidates:
                if val == cur and any(r["param"] == param and r["value"] == val for r in scan_matrix):
                    continue
                ov = {param: val}
                if param in _PAIRED:  # RRF dense/sparse 配对
                    ov[_PAIRED[param]] = round(1.0 / val, 3) if val else 1.0
                m = await retrieval_eval_service.evaluate_over_golden(db, overrides=ov, topk=topk)
                scan_matrix.append({"param": param, "value": val, **{k: m[k] for k in ("recall", "mrr", "ndcg")}})
        # 开关 A/B
        for sw in SWITCHES:
            ov = {sw: not getattr(settings, sw)}
            m = await retrieval_eval_service.evaluate_over_golden(db, overrides=ov, topk=topk)
            switches_result.append({"switch": sw, "state": ov[sw],
                                    "recall": m["recall"], "mrr": m["mrr"],
                                    "delta": round(m["recall"] - baseline["recall"], 4)})
    except Exception as e:
        degraded("tune_scan", e)

    suggestions = _build_suggestions(baseline, scan_matrix, settings.TUNE_MIN_IMPROVE)
    report = {
        "baseline": baseline, "suggestions": suggestions,
        "scanMatrix": scan_matrix, "switches": switches_result,
        "runAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": round(time.time() - t0, 1), "evalCount": len(scan_matrix) + len(switches_result) + 1,
    }
    try:
        _REPORT.write_text(__import__("json").dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        degraded("tune_report_write", e)
    try:
        from app.core import metrics
        metrics.RETRIEVAL_TUNE_TOTAL.inc()
        metrics.RETRIEVAL_BASELINE.labels("recall").set(baseline["recall"])
        metrics.RETRIEVAL_BASELINE.labels("mrr").set(baseline["mrr"])
    except Exception:
        pass
    return report


def get_tune_report() -> dict:
    try:
        import json
        return json.loads(_REPORT.read_text(encoding="utf-8")) if _REPORT.exists() else {"empty": True}
    except Exception as e:
        degraded("tune_report_read", e)
        return {"empty": True}
```

> CRAG_HIGH/LOW 扫描需 `_crag_correct`（qa_service）也接 overrides 透传——若 CRAG 扫描在 baseline 评测（只调 mixed_search，不进 qa_service）中不生效，则把 CRAG_HIGH/LOW 从 PARAM_SPACE 移除，仅扫 mixed_search 内参数（RRF/MMR/TOPK）。实现时验证 CRAG 是否在评测路径生效，不生效则移除。

- [ ] **Step 4: 跑测试验证通过**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_retrieval_tune_engine.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retrieval_tune_service.py tests/test_retrieval_tune_engine.py
git commit -m "feat(tune): 扫描引擎（扰动+开关A/B+四道护栏）+ 报告缓存"
```

---

### Task 4: 端点 + metrics + 挂路由

**Files:**
- Create: `backend/app/schemas/retrieval_tune.py`
- Create: `backend/app/routers/retrieval_tune_router.py`
- Modify: `backend/app/core/metrics.py`（加 RETRIEVAL_TUNE_TOTAL / RETRIEVAL_BASELINE + 预注册）
- Modify: `backend/app/main.py`（include_router）
- Test: `tests/test_retrieval_tune_api.py`

- [ ] **Step 1: metrics 加定义**

`metrics.py` ROUTING_MISMATCH 后加：
```python
RETRIEVAL_TUNE_TOTAL = Counter("grid_retrieval_tune_total", "检索调参扫描次数")
RETRIEVAL_BASELINE = Gauge("grid_retrieval_baseline", "检索 baseline 指标", ["metric"])
```
`init_metric_series` 末尾加：
```python
        for _m in ("recall", "mrr", "ndcg"):
            RETRIEVAL_BASELINE.labels(_m).set(0)
```

- [ ] **Step 2: 写 router**

`backend/app/schemas/retrieval_tune.py`：
```python
from pydantic import BaseModel


class TuneSuggestion(BaseModel):
    param: str; current: float | int | bool; suggested: float | int | bool
    metric: str; delta: float; confidence: str; reason: str
```

`backend/app/routers/retrieval_tune_router.py`：
```python
"""检索调参建议接口（只建议）。"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limiter import limiter
from app.core.permissions import SYSTEM_CONFIG
from app.core.response import success
from app.db.session import get_db
from app.dependencies import require_perm
from app.models.user import User

router = APIRouter(prefix="/system/retrieval", tags=["检索调参"])


@router.post("/tune")
@limiter.limit("1/minute")
async def tune(request: Request, db: AsyncSession = Depends(get_db),
               user: User = Depends(require_perm(SYSTEM_CONFIG))):
    """触发检索参数扫描（异步：复用①队列 default；未落地则后台 create_task）。"""
    from app.services import retrieval_tune_service
    try:
        from app.tasks.registry import enqueue  # ①已落地时
        await enqueue("default", "retrieval_tune_run")
        return success({"mode": "queued"}, "扫描已入队，稍后查看报告")
    except Exception:
        # ①未落地：后台 create_task
        import asyncio
        from app.db.session import AsyncSessionLocal
        async def _run():
            async with AsyncSessionLocal() as _db:
                await retrieval_tune_service.run_scan(_db)
        asyncio.create_task(_run())
        return success({"mode": "background"}, "扫描已在后台运行，稍后查看报告")


@router.get("/tune/report")
async def tune_report(user: User = Depends(require_perm(SYSTEM_CONFIG))):
    from app.services import retrieval_tune_service
    return success(retrieval_tune_service.get_tune_report(), "查询成功")
```

> 若①已落地，Task 需在 `handlers.py` 补 `retrieval_tune_run` wrapper（调 `retrieval_tune_service.run_scan`）。

- [ ] **Step 3: main.py 挂路由**

include_router 区块加：
```python
from app.routers import retrieval_tune_router
app.include_router(retrieval_tune_router.router, prefix=settings.API_PREFIX)
```

- [ ] **Step 4: 写 API 测试**

`tests/test_retrieval_tune_api.py`：
```python
import pytest

@pytest.mark.asyncio
async def test_tune_report_returns_empty_or_report(auth_client_admin):
    r = await auth_client_admin.get("/api/system/retrieval/tune/report")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "empty" in data or "baseline" in data

@pytest.mark.asyncio
async def test_tune_requires_admin(auth_client_operator):
    r = await auth_client_operator.post("/api/system/retrieval/tune")
    assert r.status_code == 403
```

- [ ] **Step 5: 跑测试 + Commit**

Run: `PYTHONPATH=backend venv/Scripts/python.exe -m pytest tests/test_retrieval_tune_api.py -v`
Expected: PASS
```bash
git add backend/app/schemas/retrieval_tune.py backend/app/routers/retrieval_tune_router.py backend/app/core/metrics.py backend/app/main.py tests/test_retrieval_tune_api.py
git commit -m "feat(tune): /system/retrieval/tune 端点 + metrics + 挂路由"
```

---

### Task 5: 前端 Admin「检索调参」tab

**Files:**
- Modify: `frontend/src/api/index.js` / `frontend/src/views/Admin.vue`

- [ ] **Step 1: api 加接口**

```javascript
export const retrievalTuneApi = {
  tune: () => request.post('/system/retrieval/tune'),
  report: () => request.get('/system/retrieval/tune/report'),
}
```

- [ ] **Step 2: Admin.vue 加 tab**

「检索调参」tab（`v-if="can('system:config')"`）：
- baseline 卡片：recall / MRR / nDCG / 无结果率 / 跑分时间（来自 `report.baseline`）
- 建议表：param / 当前 / 建议 / 指标 / 提升 / confidence + **「复制 .env 行」按钮**（`navigator.clipboard.writeText(`${s.param}=${s.suggested}`)`)
- 扫描矩阵 echarts 折线：x=参数值，y=recall，按 param 分系列（`report.scanMatrix`）
- 开关 A/B 表：switch / 翻转后 recall / delta（delta<0 标红，说明这个开关不该关）
- 「重新扫描」按钮 → `retrievalTuneApi.tune()` → 提示"已入队/后台运行" → 轮询 report

- [ ] **Step 3: 构建验证**

Run: `npm --prefix frontend run build`
Expected: build success

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/index.js frontend/src/views/Admin.vue
git commit -m "feat(tune): Admin 检索调参 tab（baseline/建议/扫描矩阵/复制.env行）"
```

---

### Task 6: 端到端验收

- [ ] **Step 1: 启动后端 + 跑扫描**

Run: `venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend --port 8001`，然后 admin 调 `POST /api/system/retrieval/tune`
Expected: 返回 `{"mode":"background"}`，日志显示扫描完成

- [ ] **Step 2: 验收清单（spec §9）**

- [ ] `mixed_search(overrides=None)` 13 caller 回归绿
- [ ] `tune_report.json` 含 baseline + suggestions + scanMatrix + switches
- [ ] 每条建议满足四道护栏
- [ ] Admin tab「复制 .env 行」可复制 `RRF_K=40`
- [ ] 扫描矩阵 echarts 折线显示 recall 拐点
- [ ] `grid_retrieval_baseline{metric}` 进 Grafana
- [ ] 百炼欠费时扫描降级，报告标注「评测不完整」，不 500

- [ ] **Step 3: Commit + 完成**

```bash
git commit --allow-empty -m "feat(tune): 检索调参建议闭环端到端验收通过"
```

---

## Self-Review（计划对 spec 覆盖核对）

| spec 章节 | 覆盖 task |
|---|---|
| §2 G1 评测service化 | T2 |
| §2 G2 扫描引擎 | T3 |
| §2 G3 报告+Admin | T3(报告)/T4(端点)/T5(前端) |
| §4.2 mixed_search overrides | T1 |
| §5 建议规则四道护栏 | T3 `_build_suggestions`（margin+最优候选+多指标confidence；样本护栏在 run_scan） |
| §6 报告/端点/metrics/配置 | T3(报告)/T4(端点+metrics)/T1(配置) |
| §7 测试 | 每 task TDD + T6 集成 |
| §8 风险 | 扫描慢=限流异步(T4)；欠费=degraded(run_scan)；13 caller=T1 单测 |
| §9 验收 | T6 清单逐条 |

**Placeholder 扫描**：无 TBD；CRAG 透传不确定已在 T3 注明"实现时验证，不生效则移除"（诚实标注，非占位）。**Type 一致**：`evaluate_over_golden(db, overrides, topk)` / `run_scan(db)` / `get_tune_report()` / `_build_suggestions(baseline, scan, min_improve)` 全链路签名一致。
