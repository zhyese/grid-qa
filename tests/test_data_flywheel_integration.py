"""数据飞轮·C4 全链路集成回归（mock，无外部服务依赖）。

验证三条闭环：
1. dislike → quality_event_bus.emit(feedback.dislike) → evidence_gap._on_dislike_gap → collect
2. governance doc_blocked → governance_propagate.propagate_handler → Milvus/Neo4j/qa_cache 清理
3. online_eval low_faith → retrieval_tune._on_eval_low → run_scan 被调

所有 DB/Milvus/Neo4j 路径 mock，确保回归无外部依赖。
"""
import asyncio
import uuid as _u


def _run(coro):
    return asyncio.run(coro)


class _FakeSession:
    """通用 AsyncSession 替身：emit/collect 落库不依赖真 DB。"""
    def __init__(self):
        self.added = []
        self.row = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def add(self, row):
        row.id = "x-" + _u.uuid4().hex[:6]
        self.added.append(row)

    async def commit(self):
        pass

    async def refresh(self, row):
        pass

    async def execute(self, stmt, *a, **kw):
        class _R:
            def scalar_one_or_none(self_): return None
            def scalars(self_): return self_
            def all(self_): return []
            def scalar(self_): return 0
            def one_or_none(self_): return None
        return _R()


def test_c4_dislike_to_evidence_gap_integration(monkeypatch):
    """闭环1: dislike → bus.emit → evidence_gap._on_dislike_gap → collect。"""
    import app.services.quality_event_bus as bus
    import app.services.evidence_gap_service as eg
    import app.config as cfg

    monkeypatch.setattr(cfg.settings, "QUALITY_BUS_ENABLE", True)
    bus.reset_subscribers()
    # 重新注册 B2 订阅（前面测试 reset 可能清掉）
    bus.subscribe("feedback.dislike", eg._on_dislike_gap)

    # patch AsyncSessionLocal 走 _FakeSession
    monkeypatch.setattr(bus, "AsyncSessionLocal", lambda: _FakeSession())
    collected = []

    async def fake_collect(query, answer, confidence, grade, action, source, tenant):
        collected.append({"query": query, "source": source, "tenant": tenant})
    monkeypatch.setattr(eg, "collect", fake_collect)

    async def go():
        await bus.emit("feedback", "dislike",
                       {"query": "主变油温过高怎么办？", "answer": "不知道"},
                       tenant="t1")
        for _ in range(30):
            await asyncio.sleep(0.02)
            if not bus._bg_tasks:
                break

    _run(go())

    assert len(collected) == 1
    assert collected[0]["query"] == "主变油温过高怎么办？"
    assert collected[0]["source"] == "feedback_dislike"
    assert collected[0]["tenant"] == "t1"


def test_c4_governance_doc_blocked_propagation(monkeypatch):
    """闭环2: governance doc_blocked → propagate_handler → Milvus/Neo4j/qa_cache 清理。"""
    import app.services.governance_propagate_service as gps
    import app.config as cfg

    monkeypatch.setattr(cfg.settings, "GOVERNANCE_PROPAGATE_ENABLE", True)
    monkeypatch.setattr(cfg.settings, "QUALITY_BUS_ENABLE", True)

    calls = []

    monkeypatch.setattr(gps.milvus_client, "delete_by_doc",
                        lambda did: calls.append(("milvus", did)))
    async def fake_neo4j(doc_id):
        calls.append(("neo4j", doc_id))
    monkeypatch.setattr(gps, "_purge_neo4j_for_doc", fake_neo4j)
    async def fake_cache(doc_id):
        calls.append(("qa_cache", doc_id))
    monkeypatch.setattr(gps, "_invalidate_qa_cache_for_doc", fake_cache)
    async def fake_bump():
        calls.append(("gov_gen_bump", True))
    monkeypatch.setattr(gps, "_bump_gov_generation", fake_bump)

    _run(gps.propagate_handler(
        "eid", "governance", "doc_blocked",
        {"doc_id": "doc-foo", "reason": "withdrawn"}, "default"))

    assert ("milvus", "doc-foo") in calls
    assert ("neo4j", "doc-foo") in calls
    assert ("qa_cache", "doc-foo") in calls
    assert ("gov_gen_bump", True) in calls


def test_c4_eval_low_to_retrieval_tune_integration(monkeypatch):
    """闭环3: online_eval low_faith → bus.emit → retrieval_tune._on_eval_low → run_scan。"""
    import app.services.quality_event_bus as bus
    import app.services.retrieval_tune_service as rts
    import app.config as cfg

    monkeypatch.setattr(cfg.settings, "QUALITY_BUS_ENABLE", True)
    monkeypatch.setattr(cfg.settings, "EVAL_TO_TUNE_ENABLE", True)
    bus.reset_subscribers()
    bus.subscribe("online_eval.low_faith", rts._on_eval_low)
    bus.subscribe("retrieval_eval.eval_low", rts._on_eval_low)

    scanned = {"n": 0}

    async def fake_scan(db):
        scanned["n"] += 1
        return {"ok": True}
    monkeypatch.setattr(rts, "run_scan", fake_scan)

    import app.db.session as dbsess
    monkeypatch.setattr(dbsess, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(bus, "AsyncSessionLocal", lambda: _FakeSession())

    async def go():
        await bus.emit("online_eval", "low_faith",
                       {"query": "test", "faithfulness": 0.3}, tenant="default")
        for _ in range(30):
            await asyncio.sleep(0.02)
            if not bus._bg_tasks:
                break

    _run(go())

    assert scanned["n"] == 1


def test_c4_full_pipeline_dislike_to_gap_with_bus_off_zero_breakage(monkeypatch):
    """防御回归：QUALITY_BUS_ENABLE=False（默认）→ emit 仅入库不派发，evidence_gap.collect 不被调。"""
    import app.services.quality_event_bus as bus
    import app.services.evidence_gap_service as eg
    import app.config as cfg

    monkeypatch.setattr(cfg.settings, "QUALITY_BUS_ENABLE", False)
    bus.reset_subscribers()
    bus.subscribe("feedback.dislike", eg._on_dislike_gap)

    monkeypatch.setattr(bus, "AsyncSessionLocal", lambda: _FakeSession())
    collected = []

    async def fake_collect(*a, **kw):
        collected.append(1)
    monkeypatch.setattr(eg, "collect", fake_collect)

    async def go():
        await bus.emit("feedback", "dislike", {"query": "q", "answer": "a"}, "t")
        await asyncio.sleep(0.05)

    _run(go())

    assert collected == []  # 开关关 → 不派发，零破坏
