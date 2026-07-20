"""数据飞轮·C1 retrieval_tune 订阅 eval_low/low_faith 单测。

订阅者：retrieval_tune_service._on_eval_low → 调 run_scan（只建议模式，不自动改参）。
开关 EVAL_TO_TUNE_ENABLE 默认关。
"""
import asyncio


def _run(coro):
    return asyncio.run(coro)


def test_c1_eval_low_triggers_scan(monkeypatch):
    """emit retrieval_eval.eval_low → _on_eval_low → run_scan 被调。"""
    import app.services.retrieval_tune_service as rts
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "EVAL_TO_TUNE_ENABLE", True)

    called = {"n": 0}

    async def fake_scan(db):
        called["n"] += 1
        return {"ok": True}
    monkeypatch.setattr(rts, "run_scan", fake_scan)

    # mock 独立 session（订阅 handler 内部开）
    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    import app.db.session as dbsess
    monkeypatch.setattr(dbsess, "AsyncSessionLocal", lambda: _FakeSession())

    _run(rts._on_eval_low("eid", "retrieval_eval", "eval_low",
                          {"recall": 0.5}, "default"))
    assert called["n"] == 1


def test_c1_low_faith_triggers_scan(monkeypatch):
    """emit online_eval.low_faith → _on_eval_low → run_scan 被调。"""
    import app.services.retrieval_tune_service as rts
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "EVAL_TO_TUNE_ENABLE", True)

    called = {"n": 0}
    async def fake_scan(db):
        called["n"] += 1
        return {"ok": True}
    monkeypatch.setattr(rts, "run_scan", fake_scan)

    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    import app.db.session as dbsess
    monkeypatch.setattr(dbsess, "AsyncSessionLocal", lambda: _FakeSession())

    _run(rts._on_eval_low("eid", "online_eval", "low_faith",
                          {"faithfulness": 0.3}, "default"))
    assert called["n"] == 1


def test_c1_disabled_no_scan(monkeypatch):
    """EVAL_TO_TUNE_ENABLE=False（默认）→ 不调 scan。"""
    import app.services.retrieval_tune_service as rts
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "EVAL_TO_TUNE_ENABLE", False)

    called = {"n": 0}
    async def fake_scan(db):
        called["n"] += 1
    monkeypatch.setattr(rts, "run_scan", fake_scan)

    _run(rts._on_eval_low("eid", "retrieval_eval", "eval_low",
                          {"recall": 0.1}, "default"))
    assert called["n"] == 0


def test_c1_scan_exception_does_not_throw(monkeypatch):
    """run_scan 抛异常 → degraded 不传播（订阅者异常不阻塞总线）。"""
    import app.services.retrieval_tune_service as rts
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "EVAL_TO_TUNE_ENABLE", True)

    async def boom(db):
        raise RuntimeError("scan failed")
    monkeypatch.setattr(rts, "run_scan", boom)

    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    import app.db.session as dbsess
    monkeypatch.setattr(dbsess, "AsyncSessionLocal", lambda: _FakeSession())

    # 不抛
    _run(rts._on_eval_low("eid", "retrieval_eval", "eval_low",
                          {"recall": 0.2}, "default"))
