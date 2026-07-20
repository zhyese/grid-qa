"""数据飞轮·B3 评测低分 emit 单测。

online_eval.eval_quality faithfulness<FAITHFULNESS_GATE → emit low_faith；
retrieval_eval.evaluate_over_golden recall<0.92 → emit eval_low。
开关 EVAL_EMIT_ENABLE 默认关。
"""
import asyncio


def _run(coro):
    return asyncio.run(coro)


def _patch_emit(monkeypatch):
    import app.services.online_eval_service as oe
    emitted = []

    async def fake_emit(source, type_, payload=None, tenant="default"):
        emitted.append((source, type_, dict(payload or {}), tenant))
        return "eid"
    monkeypatch.setattr(oe.quality_event_bus, "emit", fake_emit)
    return emitted


def test_b3_low_faithfulness_emits(monkeypatch):
    """online_eval.eval_quality faithfulness < 0.85 → emit online_eval.low_faith。"""
    import app.config as cfg
    import app.services.online_eval_service as oe
    monkeypatch.setattr(cfg.settings, "EVAL_EMIT_ENABLE", True)
    monkeypatch.setattr(cfg.settings, "FAITHFULNESS_GATE", 0.85)
    emitted = _patch_emit(monkeypatch)

    # mock judge 返回低 faithfulness（halluc=0.5 → faithfulness=0.5）
    from app.rag import judge
    async def fake_ctx(query, sources, model_type):
        return {"relevance_score": 0.9}
    async def fake_halluc(answer, sources, model_type):
        return {"hallucination": 0.5}  # faithfulness = 1 - 0.5 = 0.5 < 0.85
    monkeypatch.setattr(judge, "judge_context_relevance", fake_ctx)
    monkeypatch.setattr(judge, "judge_hallucination", fake_halluc)

    # mock _judge_completeness 返回 0.9
    async def fake_comp(q, a, m):
        return 0.9
    monkeypatch.setattr(oe, "_judge_completeness", fake_comp)

    # mock OperationLog 落库：AsyncSessionLocal 在函数内 import，patch 源模块
    import app.db.session as dbsess

    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def add(self, _): pass
        async def commit(self): pass
    monkeypatch.setattr(dbsess, "AsyncSessionLocal", lambda: _FakeSession())

    result = _run(oe.eval_quality(db=None, query="主变油温",
                                  answer="油温高", contexts=[{"chunk": "x"}]))
    assert result["faithfulness"] < 0.85
    assert any(s == "online_eval" and t == "low_faith" for s, t, p, tn in emitted)


def test_b3_high_faithfulness_no_emit(monkeypatch):
    """faithfulness >= 0.85 → 不 emit。"""
    import app.config as cfg
    import app.services.online_eval_service as oe
    import app.db.session as dbsess
    monkeypatch.setattr(cfg.settings, "EVAL_EMIT_ENABLE", True)
    monkeypatch.setattr(cfg.settings, "FAITHFULNESS_GATE", 0.85)
    emitted = _patch_emit(monkeypatch)

    from app.rag import judge
    async def fake_ctx(query, sources, model_type):
        return {"relevance_score": 0.95}
    async def fake_halluc(answer, sources, model_type):
        return {"hallucination": 0.05}  # faithfulness = 0.95 >= 0.85
    monkeypatch.setattr(judge, "judge_context_relevance", fake_ctx)
    monkeypatch.setattr(judge, "judge_hallucination", fake_halluc)
    async def fake_comp(q, a, m): return 0.95
    monkeypatch.setattr(oe, "_judge_completeness", fake_comp)

    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def add(self, _): pass
        async def commit(self): pass
    monkeypatch.setattr(dbsess, "AsyncSessionLocal", lambda: _FakeSession())

    _run(oe.eval_quality(db=None, query="q", answer="a", contexts=[{"chunk": "x"}]))
    assert emitted == []


def test_b3_disabled_no_emit(monkeypatch):
    """EVAL_EMIT_ENABLE=False（默认）→ 不 emit。"""
    import app.config as cfg
    import app.services.online_eval_service as oe
    import app.db.session as dbsess
    monkeypatch.setattr(cfg.settings, "EVAL_EMIT_ENABLE", False)
    monkeypatch.setattr(cfg.settings, "FAITHFULNESS_GATE", 0.85)
    emitted = _patch_emit(monkeypatch)

    from app.rag import judge
    async def fake_ctx(q, s, m): return {"relevance_score": 0.5}
    async def fake_halluc(a, s, m): return {"hallucination": 0.6}
    monkeypatch.setattr(judge, "judge_context_relevance", fake_ctx)
    monkeypatch.setattr(judge, "judge_hallucination", fake_halluc)
    async def fake_comp(q, a, m): return 0.5
    monkeypatch.setattr(oe, "_judge_completeness", fake_comp)

    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def add(self, _): pass
        async def commit(self): pass
    monkeypatch.setattr(dbsess, "AsyncSessionLocal", lambda: _FakeSession())

    _run(oe.eval_quality(db=None, query="q", answer="a", contexts=[{"chunk": "x"}]))
    assert emitted == []


def test_b3_retrieval_eval_low_recall_emits(monkeypatch):
    """retrieval_eval recall < 0.92 → emit retrieval_eval.eval_low。"""
    import app.config as cfg
    import app.services.retrieval_eval_service as rev
    monkeypatch.setattr(cfg.settings, "EVAL_EMIT_ENABLE", True)

    emitted = []
    async def fake_emit(source, type_, payload=None, tenant="default"):
        emitted.append((source, type_, dict(payload or {})))
        return "eid"
    # rev 模块 import 时引用 quality_event_bus.emit；patch 它
    import app.services.quality_event_bus as bus
    monkeypatch.setattr(bus, "emit", fake_emit)

    # mock retrieval_service.mixed_search 返回空（recall=0）
    async def fake_mixed(db, query, topk, overrides=None):
        return []
    monkeypatch.setattr(rev.retrieval_service, "mixed_search", fake_mixed)

    # mock _load_golden 返回 1 条
    monkeypatch.setattr(rev, "_load_golden",
                        lambda: [{"query": "q", "expect": ["doc1"], "category": "x"}])

    result = _run(rev.evaluate_over_golden(db=None, overrides=None, topk=5))
    assert result["recall"] < 0.92
    assert any(s == "retrieval_eval" and t == "eval_low" for s, t, p in emitted)
