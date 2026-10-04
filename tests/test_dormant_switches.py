"""五个"半接线"休眠开关的补测（v6 报告 P1-11：有实现、无测试 → 补齐锁定行为）。

- CRAG_NEIGHBOR_EXPAND_ENABLE：ambiguous 档邻域扩展（_expand_neighbors 行为 + 开关门）
- DEBATE_ON_LOW_CONFIDENCE_ENABLE：低置信 debate 增强（_maybe_debate_augment 门与升档）
- DISLIKE_TO_GAP_ENABLE：dislike → 质量事件总线（feedback 路由 emit 分支）
- DRILL_MISS_TO_QUALITY_ENABLE：演练遗漏动作 → 质量事件（_emit_missed_quality）
- KNOWLEDGE_GOVERNANCE_FAIL_OPEN（C5）：治理存储异常时 fail-closed / fail-open（_apply_governance_gate）
"""
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.services import qa_service, retrieval_service


# ---------- 1) CRAG_NEIGHBOR_EXPAND ----------

async def _seed_chunks(db, n=4):
    doc = Document(id="doc-nb-1", doc_name="邻域测试.pdf", minio_object="k", tenant_id="t1")
    db.add(doc)
    for i in range(n):
        db.add(Chunk(doc_id=doc.id, chunk_idx=i, content=f"chunk-{i}", char_count=7))
    await db.commit()
    return doc.id


@pytest.mark.asyncio
async def test_expand_neighbors_pulls_adjacent_chunks(test_db):
    doc_id = await _seed_chunks(test_db)
    contexts = [{"chunk": "seed", "score": 0.9, "docId": doc_id, "chunkIdx": 1, "docName": "x"}]
    out = await qa_service._expand_neighbors(test_db, contexts, window=1)
    added = [c for c in out if c.get("sources") == ["neighbor"]]
    assert {c["chunkIdx"] for c in added} == {0, 2}
    assert all(c["docId"] == doc_id for c in added)


@pytest.mark.asyncio
async def test_expand_neighbors_no_dup_and_max_add(test_db):
    doc_id = await _seed_chunks(test_db, n=6)
    contexts = [
        {"docId": doc_id, "chunkIdx": 2, "score": 0.9, "chunk": "s1"},
        {"docId": doc_id, "chunkIdx": 3, "score": 0.8, "chunk": "s2"},
    ]
    out = await qa_service._expand_neighbors(test_db, contexts, window=1)
    got = [(c["docId"], c["chunkIdx"]) for c in out]
    assert len(got) == len(set(got))  # 不重复
    assert len(out) - len(contexts) <= 4  # max_add=4 上限


@pytest.mark.asyncio
async def test_expand_neighbors_gate_off_by_default(monkeypatch):
    """开关默认关：getattr 缺省必须是 False（防误开闸，锁语义）。"""
    assert getattr(settings, "CRAG_NEIGHBOR_EXPAND_ENABLE", False) is False


# ---------- 2) DEBATE_ON_LOW_CONFIDENCE ----------

@pytest.mark.asyncio
async def test_debate_gate_off_returns_unchanged(test_db, monkeypatch):
    monkeypatch.setattr(settings, "DEBATE_ON_LOW_CONFIDENCE_ENABLE", False)
    ans, conf = await qa_service._maybe_debate_augment(
        test_db, "q", "原答案", "low", {}, None)
    assert ans == "原答案" and conf == "low"


@pytest.mark.asyncio
async def test_debate_low_confidence_augments_and_upgrades(test_db, monkeypatch):
    monkeypatch.setattr(settings, "DEBATE_ON_LOW_CONFIDENCE_ENABLE", True)

    async def fake_debate(db, query, model_type):
        return {"diagnosis": {"summary": "专家共识结论",
                              "causes": [{"name": "原因A"}, {"name": "原因B"}]},
                "debate": {"disagreements": ["分歧1"]}}

    import app.services.debate_agent_service as das
    monkeypatch.setattr(das, "debate_diagnose", fake_debate)
    ans, conf = await qa_service._maybe_debate_augment(
        test_db, "q", "原答案", "low", {}, None)
    assert ans.startswith("【多专家联合裁决")
    assert "专家共识结论" in ans and "原因A" in ans
    assert conf == "medium_high"


@pytest.mark.asyncio
async def test_debate_high_confidence_skipped(test_db, monkeypatch):
    monkeypatch.setattr(settings, "DEBATE_ON_LOW_CONFIDENCE_ENABLE", True)
    ans, conf = await qa_service._maybe_debate_augment(
        test_db, "q", "原答案", "high", {}, None)
    assert ans == "原答案" and conf == "high"


# ---------- 3) DISLIKE_TO_GAP ----------

@pytest.mark.asyncio
async def test_dislike_to_gap_emits_quality_event(test_db, monkeypatch):
    """dislike + 开关开 → 质量事件总线收到 feedback/dislike 事件。"""
    import asyncio

    from starlette.requests import Request as StarletteRequest
    import app.routers.qa as qa_router
    from app.schemas.qa import FeedbackRequest

    monkeypatch.setattr(settings, "DISLIKE_TO_GAP_ENABLE", True)
    emitted = []

    async def fake_emit(source, etype, payload, tenant="default"):
        emitted.append((source, etype, payload, tenant))

    import app.services.quality_event_bus as qeb
    monkeypatch.setattr(qeb, "emit", fake_emit)

    async def noop_record(*a, **k):
        return {}

    import app.services.feedback_service as fs
    monkeypatch.setattr(fs, "record_feedback", noop_record)
    # 路由内 create_task 的旁支（黑名单/overconfident）打桩，避免测试外泄副作用
    import app.services.feedback_optimizer_service as fos
    async def noop_async(*a, **k):
        return 0
    monkeypatch.setattr(fos, "maybe_blacklist_on_dislike", noop_async)
    monkeypatch.setattr(fos, "check_overconfident", noop_async)

    body = FeedbackRequest(query="为什么油温高", answer="答案", feedback="dislike")
    user = SimpleNamespace(username="tester", tenant_id="t1")
    req = StarletteRequest({"type": "http", "method": "POST", "path": "/qa/feedback",
                            "headers": [], "query_string": b"", "client": ("127.0.0.1", 1000)})
    await qa_router.feedback(request=req, body=body, db=test_db, user=user)
    for _ in range(6):
        await asyncio.sleep(0)  # 排空 fire-and-forget 任务
    assert any(e[0] == "feedback" and e[1] == "dislike" and e[3] == "t1" for e in emitted)


@pytest.mark.asyncio
async def test_dislike_to_gap_off_no_emit(test_db, monkeypatch):
    import asyncio

    from starlette.requests import Request as StarletteRequest
    import app.routers.qa as qa_router
    from app.schemas.qa import FeedbackRequest

    monkeypatch.setattr(settings, "DISLIKE_TO_GAP_ENABLE", False)
    emitted = []

    async def fake_emit(source, etype, payload, tenant="default"):
        emitted.append((source, etype))

    import app.services.quality_event_bus as qeb
    monkeypatch.setattr(qeb, "emit", fake_emit)

    async def noop_record(*a, **k):
        return {}

    import app.services.feedback_service as fs
    monkeypatch.setattr(fs, "record_feedback", noop_record)
    import app.services.feedback_optimizer_service as fos
    async def noop_async(*a, **k):
        return 0
    monkeypatch.setattr(fos, "maybe_blacklist_on_dislike", noop_async)
    monkeypatch.setattr(fos, "check_overconfident", noop_async)

    body = FeedbackRequest(query="为什么油温高", answer="答案", feedback="dislike")
    user = SimpleNamespace(username="tester", tenant_id="t1")
    req = StarletteRequest({"type": "http", "method": "POST", "path": "/qa/feedback",
                            "headers": [], "query_string": b"", "client": ("127.0.0.1", 1000)})
    await qa_router.feedback(request=req, body=body, db=test_db, user=user)
    for _ in range(6):
        await asyncio.sleep(0)
    assert not any(e[0] == "feedback" for e in emitted)


# ---------- 4) DRILL_MISS_TO_QUALITY ----------

def _drill_run(missed=("step-1", "step-2")):
    checklist = [{"id": "step-1", "action": "断开103开关"}, {"id": "step-2", "action": "验电"},
                 {"id": "step-3", "action": "装设接地线"}]
    score = {"missed": list(missed), "coverage": 0.4, "grade": "low"}
    return SimpleNamespace(id="run-1", checklist=checklist, scenario_name="线路停电",
                           started_by="operator1", tenant="t1"), score


@pytest.mark.asyncio
async def test_drill_miss_emits_quality_event(monkeypatch):
    monkeypatch.setattr(settings, "DRILL_MISS_TO_QUALITY_ENABLE", True)
    emitted = []

    async def fake_emit(source, etype, payload, tenant="default"):
        emitted.append((source, etype, payload, tenant))

    import app.services.quality_event_bus as qeb
    monkeypatch.setattr(qeb, "emit", fake_emit)
    from app.services import drill_service
    run, score = _drill_run()
    await drill_service._emit_missed_quality(run, score)
    assert len(emitted) == 1
    src, etype, payload, tenant = emitted[0]
    assert src == "drill" and etype == "drill_missed_action" and tenant == "t1"
    assert payload["missedActions"] == ["断开103开关", "验电"]


@pytest.mark.asyncio
async def test_drill_miss_gate_off_and_no_miss(monkeypatch):
    monkeypatch.setattr(settings, "DRILL_MISS_TO_QUALITY_ENABLE", False)
    emitted = []

    async def fake_emit(*a, **k):
        emitted.append(a)

    import app.services.quality_event_bus as qeb
    monkeypatch.setattr(qeb, "emit", fake_emit)
    from app.services import drill_service
    run, score = _drill_run()
    await drill_service._emit_missed_quality(run, score)  # 开关关：直接 return
    monkeypatch.setattr(settings, "DRILL_MISS_TO_QUALITY_ENABLE", True)
    await drill_service._emit_missed_quality(run, {"missed": [], "coverage": 1.0, "grade": "full"})
    assert emitted == []


# ---------- 5) KNOWLEDGE_GOVERNANCE_FAIL_OPEN (C5) ----------

def _pool():
    return [{"docId": "d1", "chunk": "a"}, {"docId": "d2", "chunk": "b"}]


@pytest.mark.asyncio
async def test_governance_gate_filters_blocked(test_db):
    import app.services.knowledge_governance_service as kgs

    async def fake_blocked(db, ids, tenant_id=None):
        return {"d1"}

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(kgs, "blocked_document_ids", fake_blocked)
        out = await retrieval_service._apply_governance_gate(test_db, _pool(), "t1")
        assert [h["docId"] for h in out] == ["d2"]
    finally:
        monkey.undo()


@pytest.mark.asyncio
async def test_governance_gate_fail_closed_by_default(test_db, monkeypatch):
    """治理存储异常 + 生产租户 → fail-closed 清空（默认）。"""
    import app.services.knowledge_governance_service as kgs

    async def boom(db, ids, tenant_id=None):
        raise RuntimeError("gov store down")

    monkeypatch.setattr(kgs, "blocked_document_ids", boom)
    out = await retrieval_service._apply_governance_gate(test_db, _pool(), "t1")
    assert out == []


@pytest.mark.asyncio
async def test_governance_gate_fail_open_when_enabled(test_db, monkeypatch):
    """C5：KNOWLEDGE_GOVERNANCE_FAIL_OPEN 开 → 异常时放行（fail-open）。"""
    monkeypatch.setattr(settings, "KNOWLEDGE_GOVERNANCE_FAIL_OPEN", True)
    import app.services.knowledge_governance_service as kgs

    async def boom(db, ids, tenant_id=None):
        raise RuntimeError("gov store down")

    monkeypatch.setattr(kgs, "blocked_document_ids", boom)
    pool = _pool()
    out = await retrieval_service._apply_governance_gate(test_db, pool, "t1")
    assert out == pool


@pytest.mark.asyncio
async def test_governance_gate_offline_tenant_keeps_pool(test_db, monkeypatch):
    """未传 tenant（离线评测/兼容调用）异常时维持原行为（不清空）。"""
    import app.services.knowledge_governance_service as kgs

    async def boom(db, ids, tenant_id=None):
        raise RuntimeError("gov store down")

    monkeypatch.setattr(kgs, "blocked_document_ids", boom)
    pool = _pool()
    out = await retrieval_service._apply_governance_gate(test_db, pool, None)
    assert out == pool
