"""N7 运维报告单测：数据聚合、LLM 生成/降级、生命周期。"""
import json
from datetime import datetime, timedelta

import pytest

from app.core.response import BizError
from app.models.document import Document
from app.models.ops_report import OpsReport
from app.models.ops_workbench import OpsSnapshot, TelemetryPoint
from app.models.realtime_event import RealtimeEvent
from app.models.ticket import Ticket, TicketStatus, TicketType
from app.services import ops_report_service as svc

pytestmark = pytest.mark.asyncio


def _fake_llm(monkeypatch, payload: str, fail: bool = False):
    class FakeProvider:
        async def chat(self, messages, **kw):
            if fail:
                raise ConnectionError("llm down")
            return payload
    monkeypatch.setattr("app.providers.factory.get_llm_provider", lambda m=None: FakeProvider())


async def _seed(test_db):
    now = datetime.now()
    doc = Document(id="d1", doc_name="安规.pdf", minio_object="k1", status="vectorized")
    test_db.add(doc)
    for i in range(3):
        test_db.add(RealtimeEvent(
            id=f"e{i}", tenant_id="default", event_id=f"ev{i}", source="grafana",
            event_type="alarm", severity="critical" if i == 0 else "warning",
            title=f"告警{i}", source_device_id="mt-1",
            canonical_device_name="1号主变", occurred_at=now - timedelta(hours=i)))
    test_db.add(Ticket(id="t1", tenant_id="default", ticket_type=TicketType.OPERATION,
                       status=TicketStatus.DRAFT, title="倒闸操作"))
    test_db.add(OpsSnapshot(id="s1", tenant_id="default", kind="handover",
                            title="早班交接", payload_json=json.dumps({"events": []}),
                            creator="zhang", created_at=now))
    for i in range(5):
        test_db.add(TelemetryPoint(
            id=f"p{i}", tenant_id="default", source="mock_scada", device_id="mt-1",
            metric="oil_temp", unit="℃", value=60.0 + i, quality="good",
            observed_at=now - timedelta(minutes=i), imported_by="seed"))
    await test_db.commit()


async def test_collect_data_all_sources(test_db):
    await _seed(test_db)
    data = await svc.collect_data(test_db, "default", "shift", days=1)
    src = data["sources"]
    assert src["alarms"]["total"] == 3
    assert src["alarms"]["bySeverity"]["critical"] == 1
    assert src["tickets"]["total"] == 1
    assert len(src["handovers"]) == 1
    m = src["telemetry"]["metrics"][0]
    assert m["device"] == "mt-1" and m["n"] == 5 and m["max"] == 64.0


async def test_generate_with_llm(test_db, monkeypatch):
    await _seed(test_db)
    payload = json.dumps({"sections": [{"title": "本班概览", "content": "告警3条。"}],
                          "summary": "告警3条"}, ensure_ascii=False)
    _fake_llm(monkeypatch, payload)
    row = await svc.create_report(test_db, "default", "zhang", "shift", days=1)
    await svc._generate(row["id"], "shift", {"days": 1, "deviceId": ""},
                        "default", None, "zhang", db=test_db)
    detail = await svc.get_report_detail(test_db, row["id"], "default")
    assert detail["status"] == "done"
    assert detail["sections"][0]["title"] == "本班概览"
    assert detail["llmMeta"].get("model") == "default"
    assert "本班概览" in detail["contentMd"]


async def test_generate_llm_fail_degrades_to_template(test_db, monkeypatch):
    """LLM 不可用 → 模板降级，报告仍 done 且带 fallback 标记（degraded 不 crash）。"""
    await _seed(test_db)
    _fake_llm(monkeypatch, "", fail=True)
    row = await svc.create_report(test_db, "default", "zhang", "fault", days=2)
    await svc._generate(row["id"], "fault", {"days": 2, "deviceId": ""},
                        "default", None, "zhang", db=test_db)
    detail = await svc.get_report_detail(test_db, row["id"], "default")
    assert detail["status"] == "done"
    assert detail["llmMeta"].get("fallback") is True
    assert any("降级" in s["content"] for s in detail["sections"])


async def test_invalid_type_rejected(test_db):
    with pytest.raises(BizError):
        await svc.create_report(test_db, "default", "u", "unknown_type")


async def test_list_and_delete_tenant_isolated(test_db):
    test_db.add(OpsReport(id="r1", title="A", report_type="shift", status="done",
                          tenant="default"))
    test_db.add(OpsReport(id="r2", title="B", report_type="shift", status="done",
                          tenant="other"))
    await test_db.commit()
    total, rows = await svc.list_reports(test_db, "default")
    assert total == 1 and rows[0]["id"] == "r1"
    with pytest.raises(BizError):
        await svc.delete_report(test_db, "r2", "default")


async def test_build_docx_smoke():
    detail = {"title": "测试报告", "reportType": "shift", "params": {"days": 1},
              "createdBy": "u", "createdAt": "2026-09-18T10:00:00",
              "summary": "s", "sections": [{"title": "一", "content": "内容"}]}
    blob = svc.build_report_docx(detail)
    assert blob[:2] == b"PK"  # docx 是 zip 包
