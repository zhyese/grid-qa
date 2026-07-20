"""数据飞轮·质量事件总线单测（Task A1）。"""
import asyncio
import uuid as _u


def _run(coro):
    return asyncio.run(coro)


class _FakeSession:
    """替身 AsyncSessionLocal：emit 入库不依赖真 DB。"""
    def __init__(self, store):
        self.store = store
        self.row = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def add(self, row):
        row.id = "eid-" + _u.uuid4().hex[:8]
        self.row = row
        self.store.append(row)

    async def commit(self):
        pass

    async def refresh(self, row):
        pass


def _patch_bus(monkeypatch, store):
    import app.services.quality_event_bus as bus
    monkeypatch.setattr(bus, "AsyncSessionLocal", lambda: _FakeSession(store))
    bus.reset_subscribers()
    return bus


def test_quality_bus_settings_default():
    from app.config import Settings
    s = Settings(_env_file=None)
    assert s.QUALITY_BUS_ENABLE is False  # opt-in 默认关


def test_emit_persists_event(monkeypatch):
    store = []
    bus = _patch_bus(monkeypatch, store)
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "QUALITY_BUS_ENABLE", False)
    eid = _run(bus.emit("qa_service", "refused", {"q": "x"}))
    assert eid.startswith("eid-")
    assert len(store) == 1 and store[0].source == "qa_service"


def test_no_dispatch_when_disabled(monkeypatch):
    store = []
    bus = _patch_bus(monkeypatch, store)
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "QUALITY_BUS_ENABLE", False)
    called = []

    async def handler(*a):
        called.append(a)
    bus.subscribe("feedback.*", handler)

    async def go():
        await bus.emit("feedback", "dislike", {})
        await asyncio.sleep(0.1)
    _run(go())
    assert called == []  # 关时不派发


def test_dispatches_to_matching_subscriber(monkeypatch):
    store = []
    bus = _patch_bus(monkeypatch, store)
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "QUALITY_BUS_ENABLE", True)
    called = []

    async def handler(eid, source, type, payload, tenant):
        called.append((source, type, payload))

    async def other(*a):
        called.append("other")
    bus.subscribe("feedback.*", handler)
    bus.subscribe("governance.*", other)  # 不匹配，不应被调

    async def go():
        await bus.emit("feedback", "dislike", {"q": "y"})
        for _ in range(20):
            await asyncio.sleep(0.02)
            if not bus._bg_tasks:
                break
    _run(go())
    assert len(called) == 1
    assert called[0] == ("feedback", "dislike", {"q": "y"})


def test_handler_exception_does_not_block(monkeypatch):
    store = []
    bus = _patch_bus(monkeypatch, store)
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "QUALITY_BUS_ENABLE", True)

    async def boom(*a):
        raise RuntimeError("handler down")
    bus.subscribe(".*", boom)

    async def go():
        eid = await bus.emit("online_eval", "low_faith", {})
        for _ in range(20):
            await asyncio.sleep(0.02)
            if not bus._bg_tasks:
                break
        return eid
    eid = _run(go())
    assert eid  # emit 不被 handler 异常阻塞


def test_dislike_handler_calls_collect(monkeypatch):
    """B2：feedback.dislike 事件 → evidence_gap._on_dislike_gap → collect（坏 case 进补全链）。"""
    import app.services.evidence_gap_service as eg
    called = {}

    async def fake_collect(query, answer, confidence, grade, action, source, tenant):
        called.update(query=query, source=source, tenant=tenant)
    monkeypatch.setattr(eg, "collect", fake_collect)
    _run(eg._on_dislike_gap("eid", "feedback", "dislike",
                            {"query": "主变油温", "answer": "x"}, "t1"))
    assert called.get("query") == "主变油温"
    assert called.get("source") == "feedback_dislike"
    assert called.get("tenant") == "t1"
