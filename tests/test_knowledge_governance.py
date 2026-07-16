"""知识时效与冲突治理核心单元测试（无外部服务依赖）。"""
import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.response import BizError
from app.schemas.knowledge_governance import (
    GovernanceIssueReviewRequest,
    KnowledgeMetadataUpsert,
)
from app.services import knowledge_governance_service as svc
from app.services.knowledge_governance_service import (
    ChunkSnapshot,
    DocumentSnapshot,
    IssueFinding,
)


def _metadata(now: datetime, **overrides):
    values = {
        "owner": "设备管理部",
        "applicable_region": "华东",
        "effective_at": now - timedelta(days=100),
        "expires_at": now + timedelta(days=100),
        "is_permanent": False,
        "review_interval_days": 365,
        "next_review_at": now + timedelta(days=100),
        "version_label": "V2.1",
        "version_status": "active",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _doc(doc_id: str, name: str, text: str, *, tags: str = "主变", section: str = "主变运行限值"):
    return DocumentSnapshot(
        doc_id=doc_id,
        doc_name=name,
        doc_type="运维规程",
        equipment_tags=tags,
        chunks=[ChunkSnapshot(f"c-{doc_id}", text, section)],
    )


def test_missing_metadata_finding_contains_actionable_fields():
    doc = _doc("d1", "主变规程.pdf", "主变运行要求")
    findings = svc.build_lifecycle_findings(doc, now=datetime(2026, 7, 16))
    assert [item.issue_type for item in findings] == ["metadata_missing"]
    missing = findings[0].evidence["missingFields"]
    assert {"owner", "applicableRegion", "effectiveAt", "expiryPolicy"} <= set(missing)
    assert findings[0].fingerprint == svc.build_lifecycle_findings(
        doc, now=datetime(2026, 7, 17)
    )[0].fingerprint


def test_iso_output_marks_utc_after_timezone_normalization():
    value = datetime(2026, 7, 16, 8, 30, tzinfo=timezone(timedelta(hours=8)))
    assert svc._iso(value) == "2026-07-16T00:30:00Z"


def test_expired_and_review_due_are_both_reported():
    now = datetime(2026, 7, 16, 10, 0)
    doc = _doc("d1", "主变规程.pdf", "主变运行要求")
    doc.metadata = _metadata(
        now,
        expires_at=now - timedelta(days=5),
        next_review_at=now - timedelta(days=2),
    )
    findings = svc.build_lifecycle_findings(doc, now=now, expiry_warning_days=30)
    types_found = {item.issue_type for item in findings}
    assert "expired" in types_found
    assert "review_due" in types_found
    assert "metadata_missing" not in types_found
    assert svc.effective_state(doc.metadata, now) == "expired"


def test_expiring_window_and_future_effective_state():
    now = datetime(2026, 7, 16, 10, 0)
    doc = _doc("d1", "主变规程.pdf", "主变运行要求")
    doc.metadata = _metadata(now, expires_at=now + timedelta(days=10))
    assert "expiring" in {
        item.issue_type for item in svc.build_lifecycle_findings(
            doc, now=now, expiry_warning_days=30,
        )
    }
    doc.metadata.effective_at = now + timedelta(days=1)
    assert svc.effective_state(doc.metadata, now) == "not_yet_effective"


def test_retrieval_gate_blocks_only_explicitly_invalid_knowledge():
    now = datetime(2026, 7, 16, 10, 0)
    assert svc.is_retrievable(None, now) is True
    assert svc.is_retrievable(_metadata(now, version_status="draft"), now) is True
    assert svc.is_retrievable(_metadata(now, version_status="superseded"), now) is False
    assert svc.is_retrievable(_metadata(now, version_status="withdrawn"), now) is False
    assert svc.is_retrievable(
        _metadata(now, expires_at=now - timedelta(seconds=1)), now
    ) is False
    assert svc.is_retrievable(
        _metadata(now, effective_at=now + timedelta(seconds=1)), now
    ) is False


def test_detects_explainable_negation_and_threshold_conflicts():
    left = _doc(
        "d1",
        "主变运行规程V1.pdf",
        "主变冷却器运行时必须投入。主变绕组温度不得超过80℃。",
    )
    right = _doc(
        "d2",
        "主变运行规程V2.pdf",
        "主变冷却器运行时严禁投入。主变绕组温度不得超过85℃。",
    )
    findings = svc.detect_potential_conflicts([left, right])
    by_type = {item.issue_type: item for item in findings}
    assert {"conflict_negation", "conflict_threshold"} <= set(by_type)

    negation = by_type["conflict_negation"].evidence
    assert negation["matchType"] == "normative_polarity"
    assert negation["matches"][0]["left"]["excerpt"]
    assert negation["matches"][0]["right"]["excerpt"]
    assert "潜在冲突" in negation["disclaimer"]

    threshold = by_type["conflict_threshold"].evidence["matches"][0]
    values = {
        threshold["left"]["threshold"]["value"],
        threshold["right"]["threshold"]["value"],
    }
    assert values == {80.0, 85.0}
    assert threshold["left"]["threshold"]["unit"] == "℃"


def test_different_equipment_or_topic_is_not_compared():
    breaker = _doc(
        "d1", "断路器规程.pdf", "断路器分闸时间必须符合出厂要求。",
        tags="断路器", section="断路器检修",
    )
    battery = _doc(
        "d2", "蓄电池规程.pdf", "蓄电池充电时间严禁符合出厂要求。",
        tags="蓄电池", section="蓄电池维护",
    )
    assert svc.detect_potential_conflicts([breaker, battery]) == []


def test_superseded_document_is_excluded_from_conflict_scan():
    now = datetime(2026, 7, 16)
    left = _doc("d1", "旧规程.pdf", "主变冷却器运行时必须投入。")
    left.metadata = _metadata(now, version_status="superseded")
    right = _doc("d2", "新规程.pdf", "主变冷却器运行时严禁投入。")
    right.metadata = _metadata(now)
    assert svc.detect_potential_conflicts([left, right]) == []


class _FakeScalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return _FakeScalars(self.rows)


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.added = []
        self.committed = False

    async def execute(self, _statement):
        return _FakeResult(self.rows)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.committed = True


def test_scan_refresh_does_not_overwrite_manual_issue_status():
    finding = IssueFinding(
        issue_type="expired",
        severity="critical",
        doc_id="d1",
        title="已失效",
        summary="证据更新",
        evidence={"expiresAt": "2026-01-01"},
    )
    existing = SimpleNamespace(
        fingerprint=finding.fingerprint,
        status="resolved",
        severity="warning",
        title="旧标题",
        summary="旧证据",
        evidence_json="{}",
        last_seen_at=None,
        occurrence_count=1,
        reviewer="reviewer-a",
        review_note="已完成换版",
    )
    db = _FakeDb([existing])
    created, updated = asyncio.run(svc._persist_findings(
        db, "default", [finding], datetime(2026, 7, 16),
    ))
    assert (created, updated) == (0, 1)
    assert existing.status == "resolved"
    assert existing.reviewer == "reviewer-a"
    assert existing.review_note == "已完成换版"
    assert existing.occurrence_count == 2
    assert db.committed is True


def test_new_scan_finding_is_open_by_default():
    finding = IssueFinding(
        issue_type="review_due",
        severity="warning",
        doc_id="d1",
        title="待复审",
        summary="到期",
        evidence={},
    )
    db = _FakeDb([])
    created, updated = asyncio.run(svc._persist_findings(
        db, "default", [finding], datetime(2026, 7, 16),
    ))
    assert (created, updated) == (1, 0)
    assert len(db.added) == 1
    assert db.added[0].status == "open"


def test_status_transition_requires_reopen_after_resolution():
    svc.validate_status_transition("open", "confirmed")
    svc.validate_status_transition("confirmed", "resolved")
    svc.validate_status_transition("resolved", "open")
    with pytest.raises(BizError):
        svc.validate_status_transition("resolved", "confirmed")


def test_schema_rejects_invalid_date_range_and_missing_resolution_note():
    with pytest.raises(ValidationError):
        KnowledgeMetadataUpsert(
            effectiveAt=datetime(2026, 8, 1),
            expiresAt=datetime(2026, 7, 1),
        )
    with pytest.raises(ValidationError):
        KnowledgeMetadataUpsert(isPermanent=True, expiresAt=datetime(2026, 8, 1))
    with pytest.raises(ValidationError):
        GovernanceIssueReviewRequest(status="resolved", note="")


def test_enqueue_scan_uses_persistent_task_center_facade(monkeypatch):
    captured = {}
    module = types.ModuleType("app.services.task_center_service")

    async def enqueue_task(**kwargs):
        captured.update(kwargs)
        return {"taskId": "task-1"}

    module.enqueue_task = enqueue_task
    monkeypatch.setitem(sys.modules, "app.services.task_center_service", module)
    result = asyncio.run(svc.enqueue_governance_scan(
        "tenant-a", expiry_warning_days=45, include_conflicts=True,
    ))
    assert result == {"taskId": "task-1"}
    assert captured["task_type"] == "knowledge.scan"
    assert captured["queue"] == "default"
    assert captured["tenant_id"] == "tenant-a"
    assert captured["payload"]["tenant_id"] == "tenant-a"
    assert captured["idempotency_key"].startswith("knowledge.scan:tenant-a:")


def test_task_handler_accepts_payload_and_worker_context(monkeypatch):
    captured = {}

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def fake_run_scan(db, tenant_id, **options):
        captured.update({"db": db, "tenant": tenant_id, "options": options})
        return {"documentsScanned": 0}

    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: SessionContext())
    monkeypatch.setattr(svc, "run_scan", fake_run_scan)
    context = SimpleNamespace(tenant_id="tenant-from-context")
    result = asyncio.run(svc.handle_knowledge_governance_scan(
        {"tenant_id": "spoofed", "expiry_warning_days": 45}, context,
    ))
    assert result == {"documentsScanned": 0}
    assert captured["tenant"] == "tenant-from-context"
    assert captured["options"]["expiry_warning_days"] == 45


def test_scan_status_api_is_tenant_bound(monkeypatch):
    from app.routers import knowledge_governance as router

    captured = {}
    now = datetime(2026, 7, 16, 10, 0)
    task = SimpleNamespace(
        id="task-1",
        tenant_id="tenant-a",
        queue_name="default",
        task_type="knowledge.scan",
        payload={},
        status="succeeded",
        priority=0,
        attempts=1,
        max_attempts=3,
        run_after=now,
        idempotency_key="idem",
        correlation_id="",
        causation_id="",
        locked_by="",
        last_error="",
        result={"documentsScanned": 2},
        created_at=now,
        updated_at=now,
        finished_at=now,
    )

    async def fake_get_task(db, task_id, *, tenant_id):
        captured.update({"db": db, "task_id": task_id, "tenant_id": tenant_id})
        return task

    monkeypatch.setattr(router.task_queue_service, "get_task", fake_get_task)
    response = asyncio.run(router.scan_task_status(
        "task-1",
        db=object(),
        user=SimpleNamespace(tenant_id="tenant-a"),
    ))
    assert captured["tenant_id"] == "tenant-a"
    assert response.data["id"] == "task-1"
    assert response.data["done"] is True
