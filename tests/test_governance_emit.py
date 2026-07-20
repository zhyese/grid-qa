"""数据飞轮·A2 治理状态变更 emit 单测。

upsert_metadata version_status→withdrawn/superseded → emit governance.doc_blocked；
run_scan 扫到 expired → emit governance.doc_blocked。
开关：QUALITY_BUS_ENABLE（关=现状，零 emit）。
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace


def _run(coro):
    return asyncio.run(coro)


class _FakeResult:
    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v

    def scalars(self):
        return self

    def all(self):
        return [self._v] if self._v is not None else []


class _FakeSession:
    """按 execute 调用顺序返回：第1次=Document，第2次=Meta。"""

    def __init__(self, doc, meta=None):
        self.doc = doc
        self.meta = meta
        self._calls = 0
        self.added = []

    async def execute(self, stmt):
        self._calls += 1
        if self._calls == 1:
            return _FakeResult(self.doc)
        return _FakeResult(self.meta)

    def add(self, obj):
        self.added.append(obj)
        if obj.__class__.__name__ == "KnowledgeDocumentMetadata":
            self.meta = obj

    async def commit(self):
        pass

    async def refresh(self, _obj):
        pass


def _patch_emit(monkeypatch):
    """patch governance 模块下的 quality_event_bus.emit，返回 emitted 列表。"""
    import app.services.knowledge_governance_service as gov

    emitted = []

    async def fake_emit(source, type_, payload=None, tenant="default"):
        emitted.append((source, type_, dict(payload or {}), tenant))
        return "eid-x"

    monkeypatch.setattr(gov.quality_event_bus, "emit", fake_emit)
    return emitted


def test_a2_withdraw_emits_doc_blocked(monkeypatch):
    import app.config as cfg
    import app.services.knowledge_governance_service as gov
    monkeypatch.setattr(cfg.settings, "QUALITY_BUS_ENABLE", True)

    emitted = _patch_emit(monkeypatch)
    doc = SimpleNamespace(id="d1", doc_name="规程.pdf", tenant_id="default")
    db = _FakeSession(doc=doc, meta=None)

    _run(gov.upsert_metadata(db, "d1", "default",
                             {"version_status": "withdrawn"}, "tester"))

    assert len(emitted) == 1
    src, typ, payload, tenant = emitted[0]
    assert src == "governance" and typ == "doc_blocked"
    assert payload["doc_id"] == "d1"
    assert payload["reason"] == "withdrawn"
    assert tenant == "default"


def test_a2_supersede_emits_doc_blocked(monkeypatch):
    import app.config as cfg
    import app.services.knowledge_governance_service as gov
    monkeypatch.setattr(cfg.settings, "QUALITY_BUS_ENABLE", True)

    emitted = _patch_emit(monkeypatch)
    doc = SimpleNamespace(id="d2", doc_name="规程v2.pdf", tenant_id="default")
    db = _FakeSession(doc=doc, meta=None)

    _run(gov.upsert_metadata(db, "d2", "default",
                             {"version_status": "superseded"}, "tester"))

    assert len(emitted) == 1
    assert emitted[0][0] == "governance"
    assert emitted[0][2]["reason"] == "superseded"


def test_a2_active_does_not_emit(monkeypatch):
    """version_status=active 不触发 doc_blocked（active 是正常态）。"""
    import app.config as cfg
    import app.services.knowledge_governance_service as gov
    monkeypatch.setattr(cfg.settings, "QUALITY_BUS_ENABLE", True)

    emitted = _patch_emit(monkeypatch)
    doc = SimpleNamespace(id="d3", doc_name="规程.pdf", tenant_id="default")
    db = _FakeSession(doc=doc, meta=None)

    _run(gov.upsert_metadata(db, "d3", "default",
                             {"version_status": "active",
                              "effective_at": datetime(2026, 1, 1)}, "tester"))

    assert emitted == []


def test_a2_disabled_no_emit(monkeypatch):
    """QUALITY_BUS_ENABLE=False（默认）→ 不 emit（关=现状零破坏）。"""
    import app.config as cfg
    import app.services.knowledge_governance_service as gov
    monkeypatch.setattr(cfg.settings, "QUALITY_BUS_ENABLE", False)

    emitted = _patch_emit(monkeypatch)
    doc = SimpleNamespace(id="d4", doc_name="规程.pdf", tenant_id="default")
    db = _FakeSession(doc=doc, meta=None)

    _run(gov.upsert_metadata(db, "d4", "default",
                             {"version_status": "withdrawn"}, "tester"))
    assert emitted == []


def test_a2_run_scan_emits_expired(monkeypatch):
    """run_scan 扫到 expired finding → emit governance.doc_blocked reason=expired。"""
    import app.config as cfg
    import app.services.knowledge_governance_service as gov
    monkeypatch.setattr(cfg.settings, "QUALITY_BUS_ENABLE", True)
    emitted = _patch_emit(monkeypatch)

    # mock _load_snapshots 返回一份过期文档；_persist_findings 返回 (0,0) 避免 DB
    async def fake_load(db, tenant_id, **kw):
        return [SimpleNamespace(
            doc_id="d-exp", doc_name="旧规程.pdf", doc_type="规程",
            equipment_tags="", chunks=[],
            metadata=SimpleNamespace(
                owner="x", applicable_region="", effective_at=None,
                expires_at=None, is_permanent=False, review_interval_days=None,
                next_review_at=None, version_label="", version_status="active",
            ),
        )]

    async def fake_persist(db, tenant_id, findings, now):
        return 0, 0

    monkeypatch.setattr(gov, "_load_snapshots", fake_load)
    monkeypatch.setattr(gov, "_persist_findings", fake_persist)
    # 强制过期：把 build_lifecycle_findings 替换成直接产 expired
    def fake_build(doc, now, expiry_warning_days):
        return [gov.IssueFinding(
            issue_type="expired", severity="warning", doc_id=doc.doc_id,
            title="过期", summary="文档已过期",
            evidence={"effectiveAt": "2020-01-01", "now": str(now)},
        )]
    monkeypatch.setattr(gov, "build_lifecycle_findings", fake_build)

    _run(gov.run_scan(db=None, tenant_id="default", include_conflicts=False))

    assert any(e[0] == "governance" and e[1] == "doc_blocked"
               and e[2]["doc_id"] == "d-exp" and e[2]["reason"] == "expired"
               for e in emitted)
