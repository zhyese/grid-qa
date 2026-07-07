# 证据补全闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`).

**Goal:** 把 confidence=medium/refused 的问答自动收集到 evidence_gap 表，AI 续写草稿 + 人工确认后同步入库（FAQ 文档 vectorize + qa_cache/Redis 双写），下次同问题命中不再证据有限/不足。

**Architecture:** 新表 evidence_gap（状态机 pending→ai_drafted→synced/ignored）+ Collector（answer/stream 后自动去重写）+ Reporter（Chat 上报）+ AIDrafter（放宽检索续写）+ SyncService（复用 document_service.vectorize_document + cache_set_mysql/json）。复用现有 confidence/document_service/cache 设施。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy(async) / Redis / Milvus / pytest / Vue3

## Global Constraints
- 改后端源码后 `docker compose up -d --build backend`；前端 Vite HMR
- 容器 pytest：`export MSYS_NO_PATHCONV=1` + `docker cp tests grid-backend:/app/tests`（先 rm -rf 防嵌套）+ `docker exec grid-backend python -m pytest`
- 所有降级走 `app.core.obs.degraded`；bg task 用独立 `AsyncSessionLocal`（防 session 并发 500）
- confidence 来源不变（`_crag_correct` → confidence_of：high/medium/refused）
- bg task/事件写入用独立 session；HTTP 恒 200 业务码在 body
- 建表用 `Base.metadata.create_all` 兜底 + Alembic 迁移文件

## File Structure
**新建**：`models/evidence_gap.py` / `services/evidence_gap_service.py` / `migrations/versions/xxx_add_evidence_gap.py` / `tests/test_evidence_gap*.py`
**修改**：`services/qa_service.py`（answer/stream 接 Collector）/ `routers/qa.py`（report 接口）/ `routers/system.py`（admin 接口）/ `config.py` / `views/Admin.vue`（新 tab）/ `views/Chat.vue`（上报按钮）

---

## Task 1: config + EvidenceGap model + 迁移

**Files:** Create `backend/app/models/evidence_gap.py`, `backend/migrations/versions/b2c3d4e5f6a7_add_evidence_gap.py`; Modify `backend/app/config.py`
**Interfaces:** Produces `EvidenceGap` model（字段见 spec §4）+ `settings.EVIDENCE_GAP_AUTO_COLLECT/EVIDENCE_GAP_DRAFT_TOPK_MULT/EVIDENCE_GAP_FAQ_DOCTYPE`

- [ ] **Step 1: config.py 加字段**（优化建议段后）
```python
    # ---------- 证据补全闭环 ----------
    EVIDENCE_GAP_AUTO_COLLECT: bool = True     # 自动收集 medium/refused
    EVIDENCE_GAP_DRAFT_TOPK_MULT: int = 2      # AI 续写检索放宽倍数
    EVIDENCE_GAP_FAQ_DOCTYPE: str = "证据补全FAQ"  # 同步入库的 docType
```

- [ ] **Step 2: 写 model** `backend/app/models/evidence_gap.py`
```python
"""证据补全表：medium/refused 的问答收集 + 人工兜底回流。"""
from datetime import datetime
from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class EvidenceGap(Base):
    __tablename__ = "evidence_gap"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)             # 归一化 nq
    original_answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[str] = mapped_column(String(16), default="medium")  # medium|refused
    grade: Mapped[str] = mapped_column(String(16), default="")
    crag_action: Mapped[str] = mapped_column(String(16), default="")
    source: Mapped[str] = mapped_column(String(16), default="auto")       # auto|manual
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|ai_drafted|synced|ignored
    ai_draft: Mapped[str] = mapped_column(Text, default="")
    final_answer: Mapped[str] = mapped_column(Text, default="")
    synced_doc_id: Mapped[str] = mapped_column(String(64), default="")
    synced_cache: Mapped[int] = mapped_column(Integer, default=0)
    tenant: Mapped[str] = mapped_column(String(32), default="default")
    operator: Mapped[str] = mapped_column(String(64), default="")
    handled_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
```

- [ ] **Step 3: Alembic 迁移** `backend/migrations/versions/b2c3d4e5f6a7_add_evidence_gap.py`（参照 a1b2c3d4e5f6 模式，down_revision = "a1b2c3d4e5f6"，create_table evidence_gap + 索引 ts/status）

- [ ] **Step 4: 建表 + py_compile + commit**
```bash
export MSYS_NO_PATHCONV=1
docker cp backend/app/models/evidence_gap.py grid-backend:/app/app/models/evidence_gap.py
docker exec grid-backend python -c "import asyncio;from app.db.base import Base;from app.db.session import engine;from app.models.evidence_gap import EvidenceGap;
async def g():
 async with engine.begin() as c: await c.run_sync(Base.metadata.create_all)
asyncio.run(g())"
python -m py_compile backend/app/config.py backend/app/models/evidence_gap.py
git add backend/app/config.py backend/app/models/evidence_gap.py backend/migrations/versions/b2c3d4e5f6a7_add_evidence_gap.py
git commit -m "feat(evidence-gap): T1 config + EvidenceGap model + 迁移"
```

---

## Task 2: evidence_gap_service 基础（collect 去重 + list/get）

**Files:** Create `backend/app/services/evidence_gap_service.py`, `tests/test_evidence_gap_service.py`
**Interfaces:** Produces `async collect(query, answer, confidence, grade, action, source, tenant) -> int`（去重写，返回 id/0）、`async list_gaps(status, page, size) -> dict`、`async get_gap(id) -> dict|None`

- [ ] **Step 1: 写失败测试** `tests/test_evidence_gap_service.py`
```python
import asyncio
from app.services import evidence_gap_service as svc

def test_collect_dedup():
    async def go():
        id1 = await svc.collect("测试query", "答案", "medium", "ambiguous", "normal", "auto")
        id2 = await svc.collect("测试query", "答案", "medium", "ambiguous", "normal", "auto")  # 同 query pending 去重
        assert id1 > 0
        assert id2 == 0  # 去重
    asyncio.run(go())

def test_list_gaps():
    async def go():
        r = await svc.list_gaps("pending", 1, 20)
        assert "list" in r and "total" in r
    asyncio.run(go())
```

- [ ] **Step 2: 确认失败** `docker exec grid-backend python -m pytest tests/test_evidence_gap_service.py -v` → FAIL（模块不存在）

- [ ] **Step 3: 写实现** `backend/app/services/evidence_gap_service.py`
```python
"""证据补全：收集(medium/refused 去重) + AI续写 + 人工确认 + 同步入库回流。"""
from datetime import datetime
from sqlalchemy import desc, func, select
from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.evidence_gap import EvidenceGap


async def collect(query: str, answer: str, confidence: str, grade: str, action: str,
                  source: str = "auto", tenant: str = "default") -> int:
    """去重写：同 query 已有 pending 则跳过。返回新 id（0=去重跳过）。bg task 用独立 session。"""
    try:
        async with AsyncSessionLocal() as db:
            existing = (await db.execute(
                select(EvidenceGap).where(EvidenceGap.query == query, EvidenceGap.status == "pending")
            )).scalar_one_or_none()
            if existing:
                return 0
            row = EvidenceGap(query=query, original_answer=(answer or "")[:2000],
                              confidence=confidence, grade=grade, crag_action=action,
                              source=source, tenant=tenant)
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row.id
    except Exception as e:
        degraded("evidence_gap_collect", e)
        return 0


async def list_gaps(status: str | None = None, page: int = 1, size: int = 20) -> dict:
    try:
        async with AsyncSessionLocal() as db:
            q = select(EvidenceGap).order_by(desc(EvidenceGap.ts))
            cq = select(func.count()).select_from(EvidenceGap)
            if status:
                q = q.where(EvidenceGap.status == status); cq = cq.where(EvidenceGap.status == status)
            total = (await db.execute(cq)).scalar() or 0
            rows = (await db.execute(q.offset((page-1)*size).limit(size))).scalars().all()
            return {"total": total, "list": [{
                "id": r.id, "ts": r.ts.strftime("%Y-%m-%d %H:%M") if r.ts else "",
                "query": r.query, "originalAnswer": r.original_answer[:200],
                "confidence": r.confidence, "status": r.status, "source": r.source,
                "aiDraft": r.ai_draft, "finalAnswer": r.final_answer, "syncedDocId": r.synced_doc_id,
            } for r in rows]}
    except Exception as e:
        degraded("evidence_gap_list", e)
        return {"total": 0, "list": []}


async def get_gap(gap_id: int) -> dict | None:
    try:
        async with AsyncSessionLocal() as db:
            r = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
            if not r: return None
            return {"id": r.id, "query": r.query, "confidence": r.confidence, "status": r.status,
                    "aiDraft": r.ai_draft, "finalAnswer": r.final_answer, "originalAnswer": r.original_answer}
    except Exception as e:
        degraded("evidence_gap_get", e)
        return None
```

- [ ] **Step 4: cp + pytest + commit**
```bash
docker cp backend/app/services/evidence_gap_service.py grid-backend:/app/app/services/evidence_gap_service.py
docker exec grid-backend rm -rf /app/tests && docker cp tests grid-backend:/app/tests
docker exec grid-backend python -m pytest tests/test_evidence_gap_service.py -v
git add backend/app/services/evidence_gap_service.py tests/test_evidence_gap_service.py
git commit -m "feat(evidence-gap): T2 collect 去重写 + list/get"
```

---

## Task 3: Collector 接入 qa_service

**Files:** Modify `backend/app/services/qa_service.py`（answer + stream_answer 末尾，写缓存块后）
**Interfaces:** Consumes Task 2 `collect(...)`

- [ ] **Step 1: answer 末尾接 Collector**（return result 前，confidence 已算）
在 `qa_service.answer` 的 `return result` 前插：
```python
    # 证据补全：medium/refused 自动收集（bg task，独立 session，不阻塞响应）
    if settings.EVIDENCE_GAP_AUTO_COLLECT and confidence in ("medium", "refused"):
        try:
            from app.services import evidence_gap_service
            _bg_tasks.add(asyncio.create_task(evidence_gap_service.collect(
                nq, ans, confidence, crag_grade, crag_action, "auto", tenant,
            )))
        except Exception:
            pass
```
（`_bg_tasks` 已在 qa.py 定义；qa_service 需 import——见下）

- [ ] **Step 2: qa_service 顶部加 _bg_tasks + asyncio**
`backend/app/services/qa_service.py` 顶部加：
```python
import asyncio
_bg_tasks: set = set()
```

- [ ] **Step 3: stream_answer done 前同样接 Collector**（`full` 拼完后、yield done 前）
```python
    if settings.EVIDENCE_GAP_AUTO_COLLECT and confidence in ("medium", "refused"):
        try:
            from app.services import evidence_gap_service
            asyncio.create_task(evidence_gap_service.collect(
                nq, full, confidence, crag_grade, crag_action, "auto", tenant,
            ))
        except Exception:
            pass
```

- [ ] **Step 4: py_compile + rebuild + 验证（问 medium 问题看入表）+ commit**
```bash
python -m py_compile backend/app/services/qa_service.py
docker compose up -d --build backend
# 等 ready 后问一个证据有限问题，查 evidence_gap 表有记录
git add backend/app/services/qa_service.py
git commit -m "feat(evidence-gap): T3 Collector 接入 answer/stream（medium/refused 自动收集）"
```

---

## Task 4: Reporter（Chat 上报接口）

**Files:** Modify `backend/app/routers/qa.py`；Consumes Task 2 `collect`

- [ ] **Step 1: 加上报接口**（qa.py，feedback 接口后）
```python
@router.post("/evidence-gap/report")
@limiter.limit("30/minute")
async def evidence_gap_report(
    request: Request, body: dict,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """用户主动上报证据不足（Chat 对 medium/refused 答案触发）。"""
    from app.services.evidence_gap_service import collect
    gid = await collect(
        term_service.normalize(body.get("query", "")),
        body.get("answer", ""), body.get("confidence", "medium"),
        body.get("grade", ""), body.get("action", ""), "manual", user.tenant_id,
    )
    return success({"id": gid}, "已上报" if gid else "已记录（去重）")
```

- [ ] **Step 2: py_compile + rebuild + curl 验证 + commit**
```bash
python -m py_compile backend/app/routers/qa.py
docker compose up -d --build backend
git add backend/app/routers/qa.py
git commit -m "feat(evidence-gap): T4 Chat 上报接口 /qa/evidence-gap/report"
```

---

## Task 5: AIDrafter（AI 续写草稿）

**Files:** Modify `backend/app/services/evidence_gap_service.py`（加 `ai_draft` 函数）
**Interfaces:** Produces `async ai_draft(gap_id, model_type) -> str`

- [ ] **Step 1: 加 ai_draft 函数**（evidence_gap_service.py）
```python
async def ai_draft(gap_id: int, model_type: str | None = None) -> str:
    """AI 续写草稿：放宽检索(topk×倍) 再 LLM 生成。失败返回空串。状态 pending→ai_drafted。"""
    from app.services import retrieval_service
    from app.providers.factory import get_llm_provider
    from app.rag import prompt_templates
    from app.config import settings
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
            if not row: return ""
            # 放宽检索
            topk = 5 * settings.EVIDENCE_GAP_DRAFT_TOPK_MULT
            contexts = await retrieval_service.mixed_search(db, row.query, topk, tenant=row.tenant)
            messages = prompt_templates.build_messages_with_history(row.query, contexts, [], "medium")
            draft = (await get_llm_provider(model_type).chat(messages, temperature=0.3)).strip()
            row.ai_draft = draft
            row.status = "ai_drafted"
            await db.commit()
            return draft
    except Exception as e:
        degraded("evidence_gap_ai_draft", e)
        return ""
```

- [ ] **Step 2: py_compile + commit**
```bash
python -m py_compile backend/app/services/evidence_gap_service.py
git add backend/app/services/evidence_gap_service.py
git commit -m "feat(evidence-gap): T5 AIDrafter（放宽检索续写草稿 pending→ai_drafted）"
```

---

## Task 6: SyncService（同步入库回流）

**Files:** Modify `backend/app/services/evidence_gap_service.py`（加 `confirm_and_sync`）
**Interfaces:** Produces `async confirm_and_sync(gap_id, final_answer, operator, model_type) -> dict`（一站式：确认 + 文档入库 + 缓存双写）

- [ ] **Step 1: 加 confirm_and_sync 函数**
```python
async def confirm_and_sync(gap_id: int, final_answer: str, operator: str,
                           model_type: str | None = None) -> dict:
    """人工确认 final_answer + 同步入库（FAQ文档 vectorize + qa_cache/Redis 双写）。状态→synced。"""
    import uuid
    from app.config import settings
    from app.models.document import Document
    from app.models.chunk import Chunk
    from app.services import chunk_service, document_service
    from app.services.cache_persist import cache_set_mysql
    from app.clients import redis_client
    from app.services import term_service
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
            if not row: return {"ok": False, "msg": "记录不存在"}
            nq = row.query
            # 1) 创建 FAQ 文档（不走 MinIO/parse，直接 status=parsed）
            doc_id = uuid.uuid4().hex
            doc = Document(id=doc_id, doc_name=f"FAQ:{nq[:40]}", doc_type=settings.EVIDENCE_GAP_FAQ_DOCTYPE,
                           minio_object="", file_size=len(final_answer.encode()), upload_user=operator,
                           tenant_id=row.tenant, status="parsed")
            db.add(doc)
            # 2) 切块（复用 chunk_service）
            chunks = chunk_service.split(final_answer)  # 切块函数（实现时确认签名）
            for idx, c in enumerate(chunks):
                db.add(Chunk(doc_id=doc_id, chunk_idx=idx, content=c, parent_idx=0))
            await db.commit()
            # 3) 向量化（复用 document_service.vectorize_document）
            await document_service.vectorize_document(db, doc_id)
            # 4) 缓存双写（qa_cache high + Redis）
            result = {"answer": final_answer, "retrievalSource": [], "confidence": "high",
                      "hallucinationRate": 0.0, "cached": True, "cacheLayer": "redis"}
            await cache_set_mysql(db, model_type, nq, nq, result)
            await redis_client.cache_set_json(f"qa:{model_type or 'default'}:{nq}", result, settings.QA_CACHE_TTL)
            # 5) 状态 synced
            row.final_answer = final_answer
            row.synced_doc_id = doc_id
            row.synced_cache = 1
            row.status = "synced"
            row.operator = operator
            row.handled_at = datetime.utcnow()
            await db.commit()
            return {"ok": True, "docId": doc_id}
    except Exception as e:
        degraded("evidence_gap_sync", e)
        return {"ok": False, "msg": str(e)}
```

- [ ] **Step 2: codegraph 确认 chunk_service.split / Document / Chunk 字段**（实现时 `codegraph explore "chunk_service split Document Chunk 字段"`，对齐签名）

- [ ] **Step 3: py_compile + commit**
```bash
python -m py_compile backend/app/services/evidence_gap_service.py
git add backend/app/services/evidence_gap_service.py
git commit -m "feat(evidence-gap): T6 SyncService（确认+FAQ文档入库+缓存双写→synced）"
```

---

## Task 7: system router admin 接口

**Files:** Modify `backend/app/routers/system.py`；Create `tests/test_evidence_gap_api.py`
**Interfaces:** GET /system/evidence-gap + POST {id}/ai-draft + POST {id}/confirm + POST {id}/ignore + DELETE {id}`

- [ ] **Step 1: 加 5 个 admin 接口**（system.py，evidence-gap 路由组）
```python
@router.get("/evidence-gap")
async def evidence_gap_list(status: str | None = None, page: int = 1, size: int = 20,
                            admin: User = Depends(require_admin)):
    from app.services.evidence_gap_service import list_gaps
    return success(await list_gaps(status, page, size), "查询成功")

@router.post("/evidence-gap/{gap_id}/ai-draft")
async def evidence_gap_ai_draft(gap_id: int, model_type: str | None = None,
                                admin: User = Depends(require_admin)):
    from app.services.evidence_gap_service import ai_draft
    draft = await ai_draft(gap_id, model_type)
    return success({"aiDraft": draft}, "续写完成")

@router.post("/evidence-gap/{gap_id}/confirm")
async def evidence_gap_confirm(gap_id: int, body: dict,
                               db: AsyncSession = Depends(get_db),
                               admin: User = Depends(require_admin)):
    from app.services.evidence_gap_service import confirm_and_sync
    r = await confirm_and_sync(gap_id, body.get("finalAnswer", ""), admin.username, body.get("modelType"))
    await write_log(db, admin.username, "证据补全确认", f"gap#{gap_id} ok={r.get('ok')}")
    return success(r, "已确认并同步" if r.get("ok") else "同步失败")

@router.post("/evidence-gap/{gap_id}/ignore")
async def evidence_gap_ignore(gap_id: int, db: AsyncSession = Depends(get_db),
                              admin: User = Depends(require_admin)):
    from app.models.evidence_gap import EvidenceGap
    row = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
    if row: row.status = "ignored"; await db.commit()
    return success(None, "已忽略")

@router.delete("/evidence-gap/{gap_id}")
async def evidence_gap_delete(gap_id: int, db: AsyncSession = Depends(get_db),
                              admin: User = Depends(require_admin)):
    from app.models.evidence_gap import EvidenceGap
    row = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gap_id))).scalar_one_or_none()
    if row: await db.delete(row); await db.commit()
    return success(None, "已删除")
```
（顶部需 `from sqlalchemy import select`，system.py 已有）

- [ ] **Step 2: cp + pytest + commit**
```bash
docker compose up -d --build backend
docker exec grid-backend rm -rf /app/tests && docker cp tests grid-backend:/app/tests
# 等 backend ready 后
docker exec grid-backend python -m pytest tests/test_evidence_gap_service.py -v
git add backend/app/routers/system.py
git commit -m "feat(evidence-gap): T7 admin 接口(list/ai-draft/confirm/ignore/delete)"
```

---

## Task 8: 前端（Admin 新 tab + Chat 上报按钮）

**Files:** Modify `frontend/src/views/Admin.vue`（新 tab「📝 证据补全」）+ `frontend/src/views/Chat.vue`（上报按钮）+ `frontend/src/api/index.js`（API 封装）

- [ ] **Step 1: api/index.js 加封装**
```javascript
// 证据补全（admin）
export const getEvidenceGaps = (params) => request.get('/system/evidence-gap', { params })
export const aiDraftGap = (id, modelType) => request.post(`/system/evidence-gap/${id}/ai-draft`, { modelType })
export const confirmGap = (id, finalAnswer, modelType) => request.post(`/system/evidence-gap/${id}/confirm`, { finalAnswer, modelType })
export const ignoreGap = (id) => request.post(`/system/evidence-gap/${id}/ignore`)
export const reportEvidenceGap = (query, answer, confidence, grade, action) =>
  request.post('/qa/evidence-gap/report', { query, answer, confidence, grade, action })
```

- [ ] **Step 2: Admin.vue 加 tab + 面板**（参照优化建议 tab 模式：tab 按钮 + card + 列表 + 编辑弹窗 + AI续写/确认同步按钮）
- [ ] **Step 3: Chat.vue medium/refused 答案下加「上报证据不足」按钮**（`m.confidence==='medium'||'refused'` 时显示，点击 `reportEvidenceGap`）
- [ ] **Step 4: Vite 编译验证 + commit**
```bash
curl -s http://localhost:5173/src/views/Admin.vue | grep -c "EvidenceGap\|证据补全"
curl -s http://localhost:5173/src/views/Admin.vue | grep -ci "syntaxerror"
git add frontend/src/views/Admin.vue frontend/src/views/Chat.vue frontend/src/api/index.js
git commit -m "feat(frontend): T8 Admin 证据补全 tab + Chat 上报按钮"
```

---

## Task 9: 端到端验证

**Files:** 无新文件

- [ ] **Step 1: rebuild + 触发收集**（问一个检索差的问题 → confidence=medium/refused → 查 evidence_gap 入表）
- [ ] **Step 2: AI 续写 + 确认同步**（Admin tab 点 AI续写 → 编辑确认 → 查 Document 入库 + qa_cache 写入）
- [ ] **Step 3: 下次命中验证**（同 query 再问 → cached=true confidence=high，不再 medium/refused）
- [ ] **Step 4: golden 不退化**（eval_retrieval 对比）

---

## Self-Review
**1. Spec 覆盖**：§4 表→T1 ✓ §5.1 Collector→T2+T3 ✓ §5.2 Reporter→T4 ✓ §5.3 AIDrafter→T5 ✓ §5.4/5.5 确认+Sync→T6 ✓ §7 接口→T4+T7 ✓ §8 前端→T8 ✓ §10 测试→T2+T7 ✓ §13 验收→T9 ✓
**2. Placeholder**：T6 chunk_service.split 标注"实现时 codegraph 确认签名"（唯一外部依赖，已标注），无其他 TBD
**3. 类型一致**：collect/ai_draft/confirm_and_sync/list_gaps/get_gap 签名跨任务一致 ✓
