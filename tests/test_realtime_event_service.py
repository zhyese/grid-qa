"""实时事件规范化、接入鉴权与安全门禁的轻量单测。"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.response import BizError
from app.models.realtime_event import ProactiveOpsRun, RealtimeDeviceMapping, RealtimeEvent
from app.routers.realtime_event import authenticate_realtime_ingress
from app.schemas.realtime_event import RealtimeEventIn
from app.services import realtime_event_service as service


def test_scada_event_normalizes_with_canonical_device_mapping():
    body = RealtimeEventIn(
        eventId="SCADA-001",
        source="SCADA",
        eventType="temperature_alarm",
        severity="严重",
        occurredAt=datetime(2026, 7, 16, 9, 30),
        payload={
            "deviceId": "T1_main_transformer",
            "deviceName": "旧系统1号主变",
            "alarmText": "顶层油温达到 92℃",
            "measurements": {"oilTemperature": 92},
        },
    )
    mapping = SimpleNamespace(
        active=True,
        canonical_device_id="SUB-A:T1",
        canonical_name="A站1号主变",
        device_type="main_transformer",
        station="A站",
    )

    normalized = service.normalize_event_payload(body, mapping)

    assert normalized["source"] == "scada"
    assert normalized["severity"] == "major"
    assert normalized["device"] == {
        "sourceDeviceId": "T1_main_transformer",
        "canonicalDeviceId": "SUB-A:T1",
        "canonicalName": "A站1号主变",
        "deviceType": "main_transformer",
        "station": "A站",
        "mapped": True,
    }
    assert normalized["measurements"]["oilTemperature"] == 92
    assert normalized["safety"]["controlAllowed"] is False


@pytest.mark.parametrize(
    ("source", "payload", "expected"),
    [
        ("oms", {"resourceId": "OMS-R-9"}, "OMS-R-9"),
        ("pms", {"assetId": "PMS-A-8"}, "PMS-A-8"),
        ("generic", {"equipmentId": "GEN-E-7"}, "GEN-E-7"),
    ],
)
def test_source_specific_device_id_extraction(source, payload, expected):
    body = RealtimeEventIn(eventId=f"{source}-1", source=source, payload=payload)
    assert service.extract_source_device_id(body) == expected


def test_unmapped_device_is_explicit_not_silently_guessed():
    body = RealtimeEventIn(
        eventId="PMS-1",
        source="pms",
        severity="warning",
        payload={"assetId": "ASSET-22", "assetName": "2号断路器"},
    )
    normalized = service.normalize_event_payload(body)
    assert normalized["device"]["mapped"] is False
    assert normalized["device"]["canonicalDeviceId"] == "unmapped:pms:ASSET-22"


def test_rule_gate_only_triggers_actionable_severity():
    assert service.evaluate_rule_gate({"eventType": "alarm", "severity": "critical"})[0] is True
    assert service.evaluate_rule_gate({"eventType": "alarm", "severity": "info"})[0] is False
    assert service.evaluate_rule_gate({"eventType": "recovered", "severity": "critical"})[0] is False
    assert service.evaluate_rule_gate({"eventType": "heartbeat", "severity": "major"})[0] is False


def test_hmac_signature_and_replay_window():
    raw = b'{"eventId":"evt-1"}'
    timestamp = "1000"
    signature = service.build_ingress_signature(
        "secret", timestamp, raw, tenant_id="tenant-a",
    )

    assert service.verify_ingress_signature(
        f"sha256={signature}", timestamp, raw,
        tenant_id="tenant-a", secret="secret", now_epoch=1100,
    ) is True
    assert service.verify_ingress_signature(
        signature, timestamp, raw,
        tenant_id="tenant-a", secret="secret", now_epoch=1401,
    ) is False
    assert service.verify_ingress_signature(
        signature, timestamp, b"tampered",
        tenant_id="tenant-a", secret="secret", now_epoch=1100,
    ) is False
    assert service.verify_ingress_signature(
        signature, timestamp, raw,
        tenant_id="tenant-b", secret="secret", now_epoch=1100,
    ) is False


def test_connector_token_is_scoped_and_never_falls_back_to_webhook(monkeypatch):
    for name in (
        "REALTIME_EVENT_TOKEN",
        "REALTIME_EVENT_CREDENTIAL_TENANT",
        "REALTIME_EVENT_TENANT_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(service.settings, "REALTIME_EVENT_CREDENTIAL_TENANT", "tenant-a")
    monkeypatch.setattr(service.settings, "REALTIME_EVENT_TOKEN", "tenant-a-secret")
    monkeypatch.setattr(service.settings, "REALTIME_EVENT_TENANT_TOKENS", {})
    monkeypatch.setattr(service.settings, "ALERT_WEBHOOK_TOKEN", "public-source-default")

    assert service.verify_ingress_token(
        "tenant-a-secret", tenant_id="tenant-a",
    ) is True
    assert service.verify_ingress_token(
        "tenant-a-secret", tenant_id="tenant-b",
    ) is False

    monkeypatch.setattr(service.settings, "REALTIME_EVENT_TOKEN", "")
    assert service.verify_ingress_token(
        "public-source-default", tenant_id="tenant-a",
    ) is False


def test_connector_credential_maps_are_tenant_specific(monkeypatch):
    monkeypatch.setenv(
        "REALTIME_EVENT_TENANT_TOKENS",
        '{"tenant-a":"token-a","tenant-b":"token-b"}',
    )
    monkeypatch.setenv(
        "REALTIME_EVENT_TENANT_SIGNING_SECRETS",
        '{"tenant-a":"sign-a","tenant-b":"sign-b"}',
    )
    assert service.realtime_ingress_token("tenant-a") == "token-a"
    assert service.realtime_ingress_token("tenant-b") == "token-b"
    assert service.realtime_signing_secret("tenant-a") == "sign-a"
    assert service.realtime_signing_secret("tenant-b") == "sign-b"


def test_ingress_dependency_cannot_reuse_token_for_another_tenant(monkeypatch):
    monkeypatch.setenv(
        "REALTIME_EVENT_TENANT_TOKENS", '{"tenant-a":"token-a"}',
    )

    class RequestStub:
        async def body(self):
            return b"{}"

    async def authenticate(tenant_id):
        return await authenticate_realtime_ingress(
            request=RequestStub(),
            db=None,
            authorization=None,
            x_event_token="token-a",
            x_event_signature=None,
            x_event_timestamp=None,
            x_tenant_id=tenant_id,
        )

    identity = asyncio.run(authenticate("tenant-a"))
    assert identity.tenant_id == "tenant-a"
    with pytest.raises(BizError) as exc:
        asyncio.run(authenticate("tenant-b"))
    assert exc.value.code == 401


def test_event_id_is_required_for_idempotency():
    with pytest.raises(ValidationError):
        RealtimeEventIn(eventId="   ", source="scada")


def test_ticket_draft_stays_draft_and_records_source():
    event = SimpleNamespace(
        event_id="EV-9",
        source="scada",
        title="主变温度越限",
        canonical_device_name="1号主变",
        canonical_device_id="T1",
        station="110kV站",
    )
    draft = service.normalize_ticket_draft(
        {
            "summary": "检查冷却系统",
            "ticket": {
                "ticketType": "非法控制票",
                "steps": ["检查风机", "必要时申请减载"],
                "safety": ["执行前核对设备"],
            },
        },
        event,
    )
    assert draft["ticketType"] == "操作票"
    assert draft["device"] == "1号主变"
    assert "仅为草稿" in draft["notes"]
    assert "EV-9" in draft["notes"]


def test_database_models_have_tenant_scoped_idempotency_and_read_only_defaults():
    event_constraints = {item.name for item in RealtimeEvent.__table__.constraints}
    mapping_constraints = {item.name for item in RealtimeDeviceMapping.__table__.constraints}
    assert "uq_rt_event_idempotency" in event_constraints
    assert "uq_rt_device_mapping_source" in mapping_constraints
    assert ProactiveOpsRun.__table__.c.execution_mode.default.arg == "read_only"
    assert ProactiveOpsRun.__table__.c.requires_human_review.default.arg is True
    assert ProactiveOpsRun.__table__.c.control_executed.default.arg is False


def test_realtime_router_exposes_full_human_review_loop():
    from app.routers.realtime_event import router

    paths = {route.path for route in router.routes}
    assert "/realtime/events" in paths
    assert "/realtime/runs/{run_id}/confirm" in paths
    assert "/realtime/runs/{run_id}/reject" in paths
    assert "/realtime/runs/{run_id}/to-ticket" in paths
    assert "/realtime/device-mappings" in paths


def test_task_handler_is_exported_for_task_center_registration():
    from app.tasks.registry import get_task_handler

    assert service.TASK_HANDLERS[service.TASK_TYPE] is service.proactive_ops_task_handler
    assert get_task_handler(service.TASK_TYPE) is service.proactive_ops_task_handler
