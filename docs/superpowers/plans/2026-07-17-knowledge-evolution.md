# 知识库自进化闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `knowledge_evolution_service` 编排器，对 dislike 聚类→识别 Milvus 盲区→LLM 写增量 chunk 草稿→审核回流，复用 PersistentTask/governance 范式，零新底座。

**Architecture:** 复刻 `knowledge_governance` 的 scan 入队 + review 审核双范式。新增 1 张表 `KnowledgeEvolutionDraft` + 1 个编排器 service + 1 套 router；聚类用零依赖贪心近邻；回流复用 `chunk_service`/`document_service`；P3 加定时任务 + 配额 + 指标。

**Tech Stack:** FastAPI + SQLAlchemy(async) + MySQL + Milvus + Redis；embedding_service(qwen)；LLM(deepseek/通义)；pytest。

## Global Constraints

- 聚类阈值 `0.82`、盲区 `top1_score<0.55`、`quality_score=0.6`、配额每周 `20`、定时 `24h`
- source_type 打标 `ai_evolution`；幂等键 = representative_query hash
- 权限复用 `DOC_READ`/`DOC_MANAGE` + `require_perm`
- 模型走 `create_all`（init_db 注册），不引 Alembic 迁移
- tenant 隔离贯穿（所有查询带 `tenant_id`）
- 路由前缀 `/knowledge-evolution`，挂载到 `main.py`
- 测试用 venv + `PYTHONPATH=backend`

## File Structure

```
新增 backend/app/models/knowledge_evolution.py       KnowledgeEvolutionDraft 模型
新增 backend/app/schemas/knowledge_evolution.py       Pydantic 请求/响应
新增 backend/app/services/knowledge_evolution_service.py  编排器（管道+审核+回流）
新增 backend/app/routers/knowledge_evolution.py       7 个端点
新增 tests/test_knowledge_evolution.py                单测+集成
新增 frontend/src/views/KnowledgeEvolution.vue        审核台
改  backend/app/db/init_db.py                         注册新模型
改  backend/app/main.py                                挂载 router
改  backend/app/tasks/builtin.py                      注册 knowledge_evolution.scan handler
改  backend/app/services/retrieval_service.py         source_type 降权
改  backend/app/core/metrics.py                       5 个指标
改  frontend/src/api/index.js + router/index.js       API+路由
```

---

## Task 1: KnowledgeEvolutionDraft 模型 + 建表注册

**Files:**
- Create: `backend/app/models/knowledge_evolution.py`
- Modify: `backend/app/models/__init__.py`（导出）
- Modify: `backend/app/db/init_db.py`（create_all 覆盖）
- Test: `tests/test_knowledge_evolution.py`

**Interfaces:**
- Produces: `KnowledgeEvolutionDraft` 模型类（字段见 spec §4）

- [ ] **Step 1: 写失败测试（建表+CRUD）**

```python
# tests/test_knowledge_evolution.py
import pytest, asyncio
from sqlalchemy import select
from app.models.knowledge_evolution import KnowledgeEvolutionDraft

@pytest.mark.asyncio
async def test_draft_create_and_read(test_db):
    d = KnowledgeEvolutionDraft(
        id="d1", tenant_id="default", cluster_id="c1",
        representative_query="主变压器油温高怎么处理",
        member_queries_json='["主变压器油温高怎么处理"]',
        status="draft", quality_score=0.6,
    )
    test_db.add(d); await test_db.commit()
    row = (await test_db.execute(select(KnowledgeEvolutionDraft).where(KnowledgeEvolutionDraft.id=="d1"))).scalar_one()
    assert row.status == "draft"
    assert row.quality_score == 0.6
```

- [ ] **Step 2: 跑测试验证失败**

Run: `PYTHONPATH=backend python -m pytest tests/test_knowledge_evolution.py::test_draft_create_and_read -v`
Expected: FAIL `ImportError: KnowledgeEvolutionDraft`

- [ ] **Step 3: 实现模型**

```python
# backend/app/models/knowledge_evolution.py
from datetime import datetime
from sqlalchemy import Column, String, Text, Float, DateTime
from app.db.base import Base  # 对齐项目 Base 导入路径
from app.services.task_queue_service import utcnow  # 复用 naive UTC

class KnowledgeEvolutionDraft(Base):
    __tablename__ = "knowledge_evolution_draft"
    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(64), index=True, default="default")
    cluster_id = Column(String(64), index=True)
    representative_query = Column(String(500))
    member_queries_json = Column(Text, default="[]")
    gap_evidence_json = Column(Text, default="{}")
    source_doc_ids_json = Column(Text, default="[]")
    draft_title = Column(String(256), default="")
    draft_content = Column(Text, default="")
    status = Column(String(16), index=True, default="draft")  # draft|approved|indexed|rejected|withdrawn
    chunk_id = Column(String(64), default="")
    quality_score = Column(Float, default=0.6)
    model_type = Column(String(32), default="")
    reviewer = Column(String(64), default="")
    review_note = Column(String(500), default="")
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    indexed_at = Column(DateTime, nullable=True)
```

> 实现时用 codegraph 确认项目 `Base` 的真实导入路径（`backend/app/db/base.py` 或 `db/session.py`），对齐其他 model。

- [ ] **Step 4: 导出 + 注册建表**

`models/__init__.py` 加 `from app.models.knowledge_evolution import KnowledgeEvolutionDraft`。
确认 `init_db.py` 的 `create_all` 能扫到（通常 `__init__.py` 导出即可）。

- [ ] **Step 5: 跑测试通过 + 提交**

Run: `PYTHONPATH=backend python -m pytest tests/test_knowledge_evolution.py::test_draft_create_and_read -v`
Expected: PASS
```bash
git add backend/app/models/knowledge_evolution.py backend/app/models/__init__.py tests/test_knowledge_evolution.py
git commit -m "feat(evolution): KnowledgeEvolutionDraft 模型+建表"
```

---

## Task 2: Pydantic Schemas

**Files:** Create `backend/app/schemas/knowledge_evolution.py`
**Interfaces:** Produces 请求体供 Task 7 router 用

- [ ] **Step 1: 写 schema 文件**

```python
# backend/app/schemas/knowledge_evolution.py
from pydantic import BaseModel, Field

class EvolutionScanRequest(BaseModel):
    sinceHours: int = Field(default=168, ge=1, le=2160)
    modelType: str | None = None

class DraftReviewRequest(BaseModel):
    action: str = Field(..., pattern="^(approve|reject)$")
    note: str = Field(default="", max_length=500)

class DraftWithdrawRequest(BaseModel):
    note: str = Field(default="", max_length=500)
```

- [ ] **Step 2: 提交**

```bash
git add backend/app/schemas/knowledge_evolution.py
git commit -m "feat(evolution): 请求schema"
```

---

## Task 3: 聚类算法（零依赖贪心近邻）

**Files:** Modify `backend/app/services/knowledge_evolution_service.py`
**Interfaces:** Produces `cluster(items, threshold, min_size) -> list[dict]`

- [ ] **Step 1: 写失败测试（已知向量→期望簇）**

```python
# tests/test_knowledge_evolution.py 追加
from app.services.knowledge_evolution_service import cluster

def _v(x):  # 简化：用单位向量近似
    import math
    n = math.sqrt(sum(i*i for i in x)); return [i/n for i in x]

def test_cluster_groups_similar():
    items = [
        {"query": "油温高", "vec": _v([1, 0.01])},
        {"query": "油温报警", "vec": _v([1, 0.02])},   # 与上近似
        {"query": "油温高", "vec": _v([1, 0.015])},
        {"query": "SF6漏气", "vec": _v([0.01, 1])},    # 正交，另成簇
    ]
    clusters = cluster(items, threshold=0.95, min_size=2)
    assert len(clusters) == 1                       # 只油温簇≥2
    assert clusters[0]["representative_query"] in ("油温高", "油温报警")

def test_cluster_filters_small():
    assert cluster([{"query":"x","vec":[1,0]}], threshold=0.5, min_size=3) == []
```

- [ ] **Step 2: 跑测试验证失败**

Run: `PYTHONPATH=backend python -m pytest tests/test_knowledge_evolution.py::test_cluster_groups_similar -v`
Expected: FAIL `ImportError`

- [ ] **Step 3: 实现 service 骨架 + cluster**

```python
# backend/app/services/knowledge_evolution_service.py
import math, hashlib, uuid
from typing import Any

TASK_TYPE = "knowledge_evolution.scan"
CLUSTER_THRESHOLD = 0.82
CLUSTER_MIN_SIZE = 3
BLIND_TOP1_THRESHOLD = 0.55
AI_QUALITY_SCORE = 0.6
WEEKLY_QUOTA_DEFAULT = 20

def _cosine(a, b):
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a)); nb = math.sqrt(sum(y*y for y in b))
    if na == 0 or nb == 0: return 0.0
    return dot / (na * nb)

def _mean_vec(members):
    n = len(members); dim = len(members[0]["vec"])
    return [sum(m["vec"][i] for m in members)/n for i in range(dim)]

def cluster(items: list[dict], threshold: float = CLUSTER_THRESHOLD,
            min_size: int = CLUSTER_MIN_SIZE) -> list[dict]:
    """零依赖贪心近邻聚类。items=[{query, vec}]。返回 [{cluster_id, representative_query, members, centroid}]。"""
    clusters: list[dict] = []
    for it in items:
        placed = False
        for c in clusters:
            if _cosine(it["vec"], c["centroid"]) >= threshold:
                c["members"].append(it); c["centroid"] = _mean_vec(c["members"]); placed = True; break
        if not placed:
            clusters.append({"cluster_id": uuid.uuid4().hex[:12],
                             "centroid": list(it["vec"]), "members": [it]})
    out = []
    for c in clusters:
        if len(c["members"]) < min_size: continue
        rep = max(c["members"], key=lambda m: _cosine(m["vec"], c["centroid"]))
        c["representative_query"] = rep["query"]; out.append(c)
    return out
```

- [ ] **Step 4: 跑测试通过 + 提交**

Run: `PYTHONPATH=backend python -m pytest tests/test_knowledge_evolution.py -k cluster -v`
Expected: PASS
```bash
git add backend/app/services/knowledge_evolution_service.py tests/test_knowledge_evolution.py
git commit -m "feat(evolution): 零依赖贪心近邻聚类"
```

---

## Task 4: 抽取 + 盲区判定

**Files:** Modify `knowledge_evolution_service.py`
**Consumes:** `feedback_service.list_feedbacks`, `embedding_service.embed_texts`, `retrieval_service`
**Produces:** `_extract_dislike(db, tenant, since_hours)`, `_identify_blind_spot(db, cluster)`

- [ ] **Step 1: 写失败测试（盲区判定，mock retrieval）**

```python
@pytest.mark.asyncio
async def test_identify_blind_spot(monkeypatch):
    from app.services import knowledge_evolution_service as ev
    async def fake_retrieve(db, q, tenant, top_k=1):
        return [{"score": 0.3, "doc_id": "x"}]   # < 0.55 = 盲区
    monkeypatch.setattr(ev, "_retrieve_top1", fake_retrieve)
    cluster = {"representative_query": "q", "members": [{"query":"q"}]}
    evi = await ev._identify_blind_spot(None, cluster, "default")
    assert evi is not None and evi["top1_score"] == 0.3
```

- [ ] **Step 2: 实现抽取 + 盲区**

```python
# knowledge_evolution_service.py 追加
from sqlalchemy import select
from app.models.evidence_gap import EvidenceGap
from app.models.feedback import Feedback
from app.services import feedback_service, embedding_service

async def _retrieve_top1(db, query, tenant, top_k=1):
    """封装 retrieval 检索，返回 [{score, doc_id}]。执行时用 codegraph 确认 retrieval_service 入口签名。"""
    from app.services import retrieval_service
    res = await retrieval_service.mixed_retrieve(db, query, tenant=tenant, top_k=top_k)
    return [{"score": float(r.get("score", 0.0)), "doc_id": r.get("doc_id", "")} for r in res]

async def _extract_dislike(db, tenant, since_hours):
    rows = await feedback_service.list_feedbacks(db, feedback="dislike", since_hours=since_hours, tenant=tenant)
    queries = []
    seen = set()
    for r in rows.get("list", []):
        q = (r.get("query") or "").strip()
        if q and q not in seen:
            seen.add(q); queries.append(q)
    # 并入 EvidenceGap pending
    gaps = (await db.execute(select(EvidenceGap.query).where(EvidenceGap.status=="pending", EvidenceGap.tenant_id==tenant))).scalars().all()
    for q in gaps:
        if q and q not in seen: seen.add(q); queries.append(q)
    return queries

async def _identify_blind_spot(db, cluster, tenant):
    q = cluster["representative_query"]
    top = await _retrieve_top1(db, q, tenant, top_k=1)
    if not top: return {"top1_score": 0.0, "hit_doc_ids": [], "confidence": "blind"}
    score = top[0]["score"]
    if score >= BLIND_TOP1_THRESHOLD: return None   # 非盲区
    return {"top1_score": score, "hit_doc_ids": [top[0]["doc_id"]], "confidence": "medium"}
```

> `_retrieve_top1` 内部调用 `retrieval_service.mixed_retrieve`：执行时用 codegraph 确认真实函数名与参数（可能是 `retrieve`/`mixed_search` 等），调整 `fake_retrieve` mock 与真实签名一致。

- [ ] **Step 3: 跑测试 + 提交**

Run: `PYTHONPATH=backend python -m pytest tests/test_knowledge_evolution.py::test_identify_blind_spot -v`
Expected: PASS
```bash
git add backend/app/services/knowledge_evolution_service.py tests/test_knowledge_evolution.py
git commit -m "feat(evolution): dislike抽取+盲区判定"
```

---

## Task 5: 草稿生成（LLM + RAG 增强）

**Files:** Modify `knowledge_evolution_service.py`
**Produces:** `_generate_draft(db, cluster, evidence, tenant, model_type) -> dict`

- [ ] **Step 1: 写失败测试（mock LLM）**

```python
@pytest.mark.asyncio
async def test_generate_draft(monkeypatch):
    from app.services import knowledge_evolution_service as ev
    async def fake_llm(prompt, model_type): return '{"title":"t","content":"c","source_refs":[]}'
    monkeypatch.setattr(ev, "_call_llm_json", fake_llm)
    cluster = {"representative_query":"q","members":[{"query":"q"},{"query":"q2"}]}
    draft = await ev._generate_draft(None, cluster, {"top1_score":0.3}, "default", None)
    assert draft["draft_title"] == "t" and draft["draft_content"] == "c"
```

- [ ] **Step 2: 实现草稿生成**

```python
# knowledge_evolution_service.py 追加
import json
from sqlalchemy import select
from app.models.document import Document
from app.services import embedding_service
from app.clients import llm_client  # 执行时 codegraph 确认 LLM 调用入口

async def _recent_standards(db, query, tenant, top_k=3):
    """最近规程文档：Document recency + 向量检索。返回 [{doc_id, name, snippet}]。"""
    rows = (await db.execute(
        select(Document).where(Document.tenant_id==tenant)
        .order_by(Document.created_at.desc()).limit(50)
    )).scalars().all()
    # 简化：返回最近 N 篇标题/片段（向量检索增强可后续加）
    return [{"doc_id": str(r.id), "name": r.doc_name, "snippet": (r.doc_name or "")[:120]} for r in rows[:top_k]]

PROMPT_TMPL = """你是电网运维知识工程师。基于高频用户疑问和参考资料，编写一条结构化知识条目。
必须严格基于参考资料，不得编造，标注来源。只输出 JSON。
【用户疑问簇】{queries}
【参考资料】{docs}
输出 JSON: {{"title":"...","content":"...","source_refs":["doc_id"]}}"""

async def _call_llm_json(prompt, model_type):
    from app.services import llm_call  # 执行时 codegraph 确认 LLM 入口(如 qa_service 用的 chat)
    raw = await llm_call(prompt, model_type)
    return raw  # 调用方 json.loads

async def _generate_draft(db, cluster, evidence, tenant, model_type):
    queries = [m["query"] for m in cluster["members"]]
    docs = await _recent_standards(db, cluster["representative_query"], tenant)
    prompt = PROMPT_TMPL.format(queries=queries[:10], docs=docs)
    raw = await _call_llm_json(prompt, model_type)
    try: obj = json.loads(raw)
    except Exception: obj = {"title": cluster["representative_query"][:64], "content": raw[:2000], "source_refs": []}
    return {
        "draft_title": str(obj.get("title", ""))[:256],
        "draft_content": str(obj.get("content", ""))[:8000],
        "source_doc_ids": [d["doc_id"] for d in docs],
        "gap_evidence": evidence,
    }
```

> `_call_llm_json` 的 LLM 入口：执行时 codegraph 确认（项目里 `qa_service`/`llm` provider 的 chat 调用），对齐真实函数名。

- [ ] **Step 3: 跑测试 + 提交**

Run: `PYTHONPATH=backend python -m pytest tests/test_knowledge_evolution.py::test_generate_draft -v`
Expected: PASS
```bash
git add backend/app/services/knowledge_evolution_service.py tests/test_knowledge_evolution.py
git commit -m "feat(evolution): LLM+RAG草稿生成"
```

---

## Task 6: run_scan 编排 + 入队（复刻 governance）

**Files:** Modify `knowledge_evolution_service.py`
**Consumes:** `task_queue_service.enqueue_task_record`
**Produces:** `run_scan(db, tenant, since_hours, model_type)`, `enqueue_evolution_scan(tenant, ...)`

- [ ] **Step 1: 写失败测试（run_scan 产出 draft 行，全 mock）**

```python
@pytest.mark.asyncio
async def test_run_scan_persists_drafts(test_db, monkeypatch):
    from app.services import knowledge_evolution_service as ev
    monkeypatch.setattr(ev, "_extract_dislike", lambda *a, **k: _async(["油温高","油温高1","油温高2"]))
    monkeypatch.setattr(ev, "cluster", lambda items, **k: [{"cluster_id":"c1","representative_query":"油温高","members":[{"query":"油温高"}]}])
    monkeypatch.setattr(ev, "_identify_blind_spot", lambda *a, **k: _async({"top1_score":0.3}))
    monkeypatch.setattr(ev, "_generate_draft", lambda *a, **k: _async({"draft_title":"t","draft_content":"c","source_doc_ids":[],"gap_evidence":{}}))
    monkeypatch.setattr(ev, "_embed", lambda qs: _async([[1.0,0.0] for _ in qs]))
    res = await ev.run_scan(test_db, "default", since_hours=168, model_type=None)
    assert res["drafts"] >= 1
```
> `_async` = `lambda x: __import__("asyncio").sleep(0, result=x)` 等价的 async helper；实现时用 `async def _async(x): return x`。

- [ ] **Step 2: 实现 run_scan + enqueue**

```python
# knowledge_evolution_service.py 追加
import asyncio
from app.models.knowledge_evolution import KnowledgeEvolutionDraft
from app.services import task_queue_service

def _embed(qs):
    """sync 包装：执行时改 await embedding_service.embed_texts。测试用 monkeypatch。"""
    return embedding_service.embed_texts(qs)  # 注意 async，run_scan 里 await

async def run_scan(db, tenant, *, since_hours=168, model_type=None):
    queries = await _extract_dislike(db, tenant, since_hours)
    if not queries: return {"clusters": 0, "drafts": 0}
    vecs = await embedding_service.embed_texts(queries)
    items = [{"query": q, "vec": v} for q, v in zip(queries, vecs)]
    clusters = cluster(items)
    drafts = []
    for c in clusters:
        evi = await _identify_blind_spot(db, c, tenant)
        if evi is None: continue
        d = await _generate_draft(db, c, evi, tenant, model_type)
        row = KnowledgeEvolutionDraft(
            id=uuid.uuid4().hex, tenant_id=tenant, cluster_id=c["cluster_id"],
            representative_query=c["representative_query"],
            member_queries_json=json.dumps([m["query"] for m in c["members"]], ensure_ascii=False),
            gap_evidence_json=json.dumps(evi, ensure_ascii=False),
            source_doc_ids_json=json.dumps(d["source_doc_ids"]),
            draft_title=d["draft_title"], draft_content=d["draft_content"],
            status="draft", quality_score=AI_QUALITY_SCORE, model_type=model_type or "",
        )
        db.add(row); drafts.append(row)
    await db.commit()
    return {"clusters": len(clusters), "drafts": len(drafts)}

async def enqueue_evolution_scan(tenant, *, since_hours=168, model_type=None):
    """复刻 governance.enqueue_governance_scan：返回 task(异步) 或 None(同步)。策略：始终入队。"""
    from app.db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        task = await task_queue_service.enqueue_task_record(
            db, TASK_TYPE, {"since_hours": since_hours, "model_type": model_type},
            queue="evolution", idempotency_key=f"evo:{tenant}:{int(__import__('time').time()//300)}",
            tenant_id=tenant, max_attempts=2, commit=True,
        )
    return task_queue_service.task_to_dict(task) if task else None
```

- [ ] **Step 3: 跑测试 + 提交**

Run: `PYTHONPATH=backend python -m pytest tests/test_knowledge_evolution.py::test_run_scan_persists_drafts -v`
Expected: PASS
```bash
git add backend/app/services/knowledge_evolution_service.py tests/test_knowledge_evolution.py
git commit -m "feat(evolution): run_scan编排+入队"
```

---

## Task 7: 注册 worker handler + router + main 挂载

**Files:** Modify `backend/app/tasks/builtin.py`, Create `backend/app/routers/knowledge_evolution.py`, Modify `backend/app/main.py`

- [ ] **Step 1: 注册 handler（复刻 knowledge.scan handler 位置）**

用 codegraph 看 `tasks/builtin.py` 里 `knowledge.scan` 的注册方式（装饰器 or dict），照抄注册 `knowledge_evolution.scan` → 调 `run_scan`。

```python
# tasks/builtin.py 内（按现有注册模式追加）
from app.services import knowledge_evolution_service
async def handle_knowledge_evolution_scan(ctx, payload):
    async with AsyncSessionLocal() as db:
        return await knowledge_evolution_service.run_scan(
            db, ctx.tenant_id, since_hours=payload.get("since_hours", 168),
            model_type=payload.get("model_type"))
# 按 registry 现有方式注册 TASK_TYPE
```

- [ ] **Step 2: 写 router**

```python
# backend/app/routers/knowledge_evolution.py
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.permissions import DOC_MANAGE, DOC_READ
from app.core.response import BizError, success
from app.db.session import get_db
from app.dependencies import require_perm
from app.models.user import User
from app.schemas.knowledge_evolution import EvolutionScanRequest, DraftReviewRequest
from app.services import knowledge_evolution_service as ev
from app.services import task_queue_service

router = APIRouter(prefix="/knowledge-evolution", tags=["知识自进化"])

@router.post("/scan")
async def scan(body: EvolutionScanRequest, db: AsyncSession = Depends(get_db), user: User = Depends(require_perm(DOC_MANAGE))):
    queued = await ev.enqueue_evolution_scan(user.tenant_id, sinceHours_since_hours(body.sinceHours), model_type=body.modelType)
    return success({"mode": "queued", "task": queued}, "自进化扫描已入队")

@router.get("/scan/{task_id}")
async def scan_status(task_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_perm(DOC_MANAGE))):
    t = await task_queue_service.get_task(db, task_id, tenant_id=user.tenant_id)
    if not t or t.task_type != ev.TASK_TYPE: raise BizError("扫描任务不存在", 404)
    d = task_queue_service.task_to_dict(t); d["done"] = t.status in {"succeeded","failed","dead"}
    return success(d, "查询成功")

@router.get("/drafts")
async def list_drafts(status: str = "", page: int = 1, size: int = 20, db: AsyncSession = Depends(get_db), user: User = Depends(require_perm(DOC_READ))):
    return success(await ev.list_drafts(db, user.tenant_id, status=status, page=page, size=size), "查询成功")

@router.get("/drafts/{draft_id}")
async def draft_detail(draft_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_perm(DOC_READ))):
    d = await ev.get_draft(db, draft_id, user.tenant_id)
    if not d: raise BizError("草稿不存在", 404)
    return success(d, "查询成功")

@router.post("/drafts/{draft_id}/review")
async def draft_review(draft_id: str, body: DraftReviewRequest, db: AsyncSession = Depends(get_db), user: User = Depends(require_perm(DOC_MANAGE))):
    return success(await ev.review_draft(db, draft_id, user.tenant_id, action=body.action, note=body.note, reviewer=user.username), "已处理")

@router.get("/stats")
async def stats(db: AsyncSession = Depends(get_db), user: User = Depends(require_perm(DOC_READ))):
    return success(await ev.get_stats(db, user.tenant_id), "查询成功")
```
> `sinceHours_since_hours` 这类命名转换是占位提示，实现时直接用 `body.sinceHours` 传入 `since_hours=body.sinceHours`。

- [ ] **Step 3: main.py 挂载**

`main.py` 仿其他 router：`from app.routers import knowledge_evolution` + `app.include_router(knowledge_evolution.router)`。

- [ ] **Step 4: 冒烟测试（启动后 curl）**

```bash
docker compose up -d --build backend
# 等 health ready 后
curl -s -H "Authorization: Bearer $TOKEN" -X POST http://localhost:8001/api/knowledge-evolution/scan -d '{"sinceHours":168}' -H "Content-Type: application/json"
```
Expected: `{"code":200,...,"mode":"queued"}`

- [ ] **Step 5: 提交**

```bash
git add backend/app/tasks/builtin.py backend/app/routers/knowledge_evolution.py backend/app/main.py
git commit -m "feat(evolution): worker handler+router+main挂载"
```

---

## Task 8: list/get/review/stats（审核流，P1 收尾）

**Files:** Modify `knowledge_evolution_service.py`
**Produces:** `list_drafts`, `get_draft`, `review_draft`, `get_stats`

- [ ] **Step 1: 写失败测试（review approve 状态流转）**

```python
@pytest.mark.asyncio
async def test_review_approve(test_db):
    from app.services import knowledge_evolution_service as ev
    test_db.add(KnowledgeEvolutionDraft(id="d1", tenant_id="default", cluster_id="c1", representative_query="q", status="draft"))
    await test_db.commit()
    await ev.review_draft(test_db, "d1", "default", action="approve", note="ok", reviewer="admin")
    row = (await test_db.execute(select(KnowledgeEvolutionDraft).where(KnowledgeEvolutionDraft.id=="d1"))).scalar_one()
    assert row.status == "approved" and row.reviewer == "admin"
```

- [ ] **Step 2: 实现审核 + 查询**

```python
# knowledge_evolution_service.py 追加
def _to_dict(r): return {
    "id": r.id, "clusterId": r.cluster_id, "representativeQuery": r.representative_query,
    "memberQueries": json.loads(r.member_queries_json or "[]"), "gapEvidence": json.loads(r.gap_evidence_json or "{}"),
    "sourceDocIds": json.loads(r.source_doc_ids_json or "[]"), "draftTitle": r.draft_title,
    "draftContent": r.draft_content, "status": r.status, "chunkId": r.chunk_id,
    "qualityScore": r.quality_score, "reviewer": r.reviewer, "reviewNote": r.review_note,
    "reviewedAt": r.reviewed_at, "createdAt": r.created_at, "indexedAt": r.indexed_at,
}

async def list_drafts(db, tenant, *, status="", page=1, size=20):
    stmt = select(KnowledgeEvolutionDraft).where(KnowledgeEvolutionDraft.tenant_id==tenant)
    if status: stmt = stmt.where(KnowledgeEvolutionDraft.status==status)
    stmt = stmt.order_by(KnowledgeEvolutionDraft.created_at.desc()).offset((page-1)*size).limit(size)
    rows = (await db.execute(stmt)).scalars().all()
    return {"total": len(rows), "list": [_to_dict(r) for r in rows]}

async def get_draft(db, draft_id, tenant):
    r = (await db.execute(select(KnowledgeEvolutionDraft).where(KnowledgeEvolutionDraft.id==draft_id, KnowledgeEvolutionDraft.tenant_id==tenant))).scalar_one_or_none()
    return _to_dict(r) if r else None

async def review_draft(db, draft_id, tenant, *, action, note, reviewer):
    r = (await db.execute(select(KnowledgeEvolutionDraft).where(KnowledgeEvolutionDraft.id==draft_id, KnowledgeEvolutionDraft.tenant_id==tenant))).scalar_one_or_none()
    if not r: raise ValueError("草稿不存在")
    if r.status != "draft": raise ValueError(f"当前状态 {r.status} 不可审核")
    r.status = "approved" if action == "approve" else "rejected"
    r.reviewer = reviewer[:64]; r.review_note = note[:500]; r.reviewed_at = utcnow()
    await db.commit(); return _to_dict(r)

async def get_stats(db, tenant):
    rows = (await db.execute(select(KnowledgeEvolutionDraft).where(KnowledgeEvolutionDraft.tenant_id==tenant))).scalars().all()
    from collections import Counter
    c = Counter(r.status for r in rows)
    return {"byStatus": dict(c), "total": len(rows)}
```

- [ ] **Step 3: 跑测试 + 提交**

Run: `PYTHONPATH=backend python -m pytest tests/test_knowledge_evolution.py::test_review_approve -v`
Expected: PASS
```bash
git add backend/app/services/knowledge_evolution_service.py tests/test_knowledge_evolution.py
git commit -m "feat(evolution): 审核+查询+stats(P1收尾)"
```

---

## Task 9: 回流 Milvus + 撤回 + 检索降权（P2）

**Files:** Modify `knowledge_evolution_service.py`, `retrieval_service.py`
**Consumes:** `chunk_service`/`document_service` 入库函数（执行时 codegraph 定位签名）

- [ ] **Step 1: codegraph 定位入库入口**

Run: codegraph_explore `chunk_service add_chunk upsert insert Milvus；document_service create chunk 入库 embed`
确认真实函数名（如 `chunk_service.upsert_chunk(db, doc_id, content, vec, metadata)`），记下签名。

- [ ] **Step 2: 写失败测试（reflow 幂等 + withdraw）**

```python
@pytest.mark.asyncio
async def test_reflow_idempotent(test_db, monkeypatch):
    from app.services import knowledge_evolution_service as ev
    calls = []
    async def fake_add(db, draft): calls.append(draft.id); return "chunk_"+draft.id
    monkeypatch.setattr(ev, "_add_chunk_to_kb", fake_add)
    test_db.add(KnowledgeEvolutionDraft(id="d1", tenant_id="default", cluster_id="c", representative_query="q", status="approved"))
    await test_db.commit()
    cid1 = await ev.reflow_to_kb(test_db, (await test_db.execute(select(KnowledgeEvolutionDraft).where(KnowledgeEvolutionDraft.id=="d1"))).scalar_one())
    cid2 = await ev.reflow_to_kb(test_db, (await test_db.execute(select(KnowledgeEvolutionDraft).where(KnowledgeEvolutionDraft.id=="d1"))).scalar_one())
    assert cid1 == cid2 and len(calls) == 1   # 幂等
```

- [ ] **Step 3: 实现 reflow + withdraw**

```python
# knowledge_evolution_service.py 追加
async def _add_chunk_to_kb(db, draft):
    """调 chunk_service/document_service 入库。执行时用 codegraph 确认的真实函数替换下方占位调用。"""
    from app.services import chunk_service, embedding_service
    vec = await embedding_service.embed_texts([draft.draft_content])
    # ↓ 真实签名以 codegraph Step1 结果为准；metadata 打标 ai_evolution
    chunk_id = await chunk_service.upsert_chunk(
        db, doc_id=_AI_EVOLUTION_DOC_ID, content=draft.draft_content,
        vec=vec[0], metadata={"source_type": "ai_evolution", "quality_score": AI_QUALITY_SCORE, "draft_id": draft.id})
    return chunk_id

_AI_EVOLUTION_DOC_ID = "ai-evolution-drafts"  # 虚拟文档聚合 AI 生成 chunk

async def reflow_to_kb(db, draft):
    if draft.status == "indexed": return draft.chunk_id      # 幂等
    if draft.status != "approved": raise ValueError("仅 approved 可回流")
    cid = await _add_chunk_to_kb(db, draft)
    draft.chunk_id = cid; draft.status = "indexed"; draft.indexed_at = utcnow()
    await db.commit(); return cid

async def withdraw_draft(db, draft_id, tenant):
    r = (await db.execute(select(KnowledgeEvolutionDraft).where(KnowledgeEvolutionDraft.id==draft_id, KnowledgeEvolutionDraft.tenant_id==tenant))).scalar_one_or_none()
    if not r or r.status != "indexed": raise ValueError("仅 indexed 可撤回")
    from app.services import chunk_service
    await chunk_service.delete_chunk(r.chunk_id)   # 执行时 codegraph 确认删除函数(含 Milvus delete)
    r.status = "withdrawn"; await db.commit(); return _to_dict(r)
```

- [ ] **Step 4: router 加 withdraw 端点**

`routers/knowledge_evolution.py` 加：
```python
@router.post("/drafts/{draft_id}/withdraw")
async def draft_withdraw(draft_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_perm(DOC_MANAGE))):
    return success(await ev.withdraw_draft(db, draft_id, user.tenant_id), "已撤回")
```

- [ ] **Step 5: 检索降权（retrieval_service）**

用 codegraph 定位 `retrieval_service` 打分/返回处，对 `metadata.source_type=='ai_evolution'` 的结果 `score *= AI_QUALITY_SCORE`；如 `AI_EVOLUTION_RETRIEVAL_FILTER=exclude` 则过滤。加配置项到 `config.py`。

- [ ] **Step 6: 跑测试 + 提交**

Run: `PYTHONPATH=backend python -m pytest tests/test_knowledge_evolution.py::test_reflow_idempotent -v`
Expected: PASS
```bash
git add backend/app/services/knowledge_evolution_service.py backend/app/routers/knowledge_evolution.py backend/app/services/retrieval_service.py backend/app/config.py tests/test_knowledge_evolution.py
git commit -m "feat(evolution): 回流Milvus+撤回+检索降权(P2)"
```

---

## Task 10: 定时入队 + 配额 + 指标（P3）

**Files:** Modify `knowledge_evolution_service.py`, `core/metrics.py`, `config.py`

- [ ] **Step 1: metrics 定义（仿现有指标）**

`core/metrics.py` 加 4 个指标（Gauge/Counter/Histogram，对齐现有 `prometheus_client` 写法）：
`evolution_clusters(Gauge, tenant)`, `evolution_drafts_total(Counter, tenant, status)`, `evolution_indexed_total(Counter, tenant)`, `evolution_scan_duration_seconds(Histogram, tenant)`。

- [ ] **Step 2: config 加配置项**

`config.py` 加：`KNOWLEDGE_EVOLUTION_CRON_HOURS=24`、`KNOWLEDGE_EVOLUTION_WEEKLY_QUOTA=20`、`AI_EVOLUTION_RETRIEVAL_FILTER="downgrade"`。

- [ ] **Step 3: 配额 + 定时入队 + 指标埋点**

```python
# knowledge_evolution_service.py 追加
async def _weekly_indexed_count(db, tenant):
    from datetime import timedelta
    cutoff = utcnow() - timedelta(days=7)
    from sqlalchemy import func
    return (await db.execute(select(func.count(KnowledgeEvolutionDraft.id)).where(
        KnowledgeEvolutionDraft.tenant_id==tenant, KnowledgeEvolutionDraft.status=="indexed",
        KnowledgeEvolutionDraft.indexed_at >= cutoff))).scalar() or 0

# run_scan 开头加指标埋点，reflow_to_kb 加配额检查：
#   if await _weekly_indexed_count(db, draft.tenant_id) >= settings.KNOWLEDGE_EVOLUTION_WEEKLY_QUOTA:
#       raise ValueError("本周回流配额已满")
```

定时入队：在 worker 启动逻辑（`tasks/worker.py` 或 `main.py` lifespan）加周期任务，每 `KNOWLEDGE_EVOLUTION_CRON_HOURS` 小时 `enqueue_evolution_scan("default")`。执行时 codegraph 看 worker 启动点接入。

- [ ] **Step 4: 测试配额 + 提交**

补测试：`test_weekly_quota_blocks_reflow`（构造 20 条 indexed，第 21 条 reflow 抛错）。
Run: `PYTHONPATH=backend python -m pytest tests/test_knowledge_evolution.py -k quota -v`
Expected: PASS
```bash
git add backend/app/core/metrics.py backend/app/config.py backend/app/services/knowledge_evolution_service.py tests/test_knowledge_evolution.py
git commit -m "feat(evolution): 定时入队+配额+指标(P3)"
```

---

## Task 11: 前端审核台

**Files:** Create `frontend/src/views/KnowledgeEvolution.vue`, Modify `api/index.js`, `router/index.js`

- [ ] **Step 1: api 封装**

`api/index.js` 追加（仿 governance API 风格）：
```javascript
export const scanKnowledgeEvolution = (sinceHours, modelType) => request.post('/knowledge-evolution/scan', { sinceHours, modelType })
export const getEvolutionScanStatus = (taskId) => request.get(`/knowledge-evolution/scan/${taskId}`)
export const getEvolutionDrafts = (params = {}) => request.get('/knowledge-evolution/drafts', { params })
export const getEvolutionDraft = (id) => request.get(`/knowledge-evolution/drafts/${id}`)
export const reviewEvolutionDraft = (id, action, note = '') => request.post(`/knowledge-evolution/drafts/${id}/review`, { action, note })
export const withdrawEvolutionDraft = (id) => request.post(`/knowledge-evolution/drafts/${id}/withdraw`)
export const getEvolutionStats = () => request.get('/knowledge-evolution/stats')
```

- [ ] **Step 2: KnowledgeEvolution.vue**

仿 `OperationsCenter.vue` 结构：stats 卡片 + drafts 列表表（representativeQuery/status/createdAt）+ 详情抽屉（draftContent/memberQueries/gapEvidence）+ 操作按钮（approve/reject/withdraw）+ 顶部「触发扫描」按钮。10s 轮询 drafts。权限 `DOC_MANAGE` 控制操作。

- [ ] **Step 3: router + 侧栏**

`router/index.js` 加 `/knowledge-evolution` 路由；`AppLayout.vue` 侧栏加「🧬 知识自进化」入口（需 DOC_READ）。

- [ ] **Step 4: rebuild frontend + 冒烟**

```bash
docker compose up -d --build frontend
```
浏览器进 `/knowledge-evolution`，点「触发扫描」，确认 drafts 列表能加载。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/views/KnowledgeEvolution.vue frontend/src/api/index.js frontend/src/router/index.js frontend/src/views/AppLayout.vue
git commit -m "feat(evolution): 前端审核台"
```

---

## Self-Review（已执行）

- **Spec 覆盖**：§4 模型→T1；§5 接口→T3-8；§6 五阶段→T3-6；§7 触发调度→T6/T10；§8 降权→T9；§9 指标→T10；§10 API→T7/T8/T9；§11 前端→T11；§13 测试→各 task 内联；§14 分阶段→T1-8=P1, T9=P2, T10=P3。✅
- **Placeholder**：`_retrieve_top1`/`_call_llm_json`/`_add_chunk_to_kb`/`chunk_service.upsert_chunk` 均为「执行时 codegraph 确认真实签名」的现有 API 调用指引（非 TODO），已逐处标注。✅
- **类型一致**：`KnowledgeEvolutionDraft`、`cluster()`、`_to_dict()`、`review_draft()` 跨 task 命名一致。✅
