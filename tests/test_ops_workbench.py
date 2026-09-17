"""五项功能的确定性、权限隔离和状态流转回归，不需要外部服务。"""

from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.response import BizError
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.knowledge_governance import KnowledgeDocumentMetadata
from app.models.ops_workbench import TelemetryPoint
from app.models.realtime_event import RealtimeDeviceMapping, RealtimeEvent
from app.models.ticket import Ticket, TicketStatus
from app.schemas.ops_workbench import (
    HandoverRequest,
    ImpactRequest,
    ImportRequest,
    ReviewRequest,
    TableQuery,
    TelemetryQuery,
    TimeRange,
)
from app.services import device_dossier_service as devices
from app.services import ops_catalog_service as catalog
from app.services import ops_snapshot_service as snapshots
from app.services import regulation_impact_service as impacts
from app.services import shift_handover_service as shifts
from app.services import table_lookup_service as tables
from app.services import telemetry_history_service as telemetry


def user(role="admin", tenant="a", name="alice", dept="ops"):
    return SimpleNamespace(role=role, tenant_id=tenant, username=name, dept=dept)


def point(**kwargs):
    return {
        "source": "historian",
        "device_id": "T1",
        "metric": "temperature",
        "unit": "℃",
        "value": 10,
        "quality": "good",
        "observed_at": "2026-09-14T08:00:00+08:00",
        **kwargs,
    }


def query(**kwargs):
    return TelemetryQuery(
        source="historian",
        device_id="T1",
        metric="temperature",
        start="2026-09-14T00:00:00Z",
        end="2026-09-15T00:00:00Z",
        **kwargs,
    )


async def seed_doc(
    db,
    doc_id="d1",
    tenant="a",
    dept="",
    text="| 型号 | 温度（℃） |\n|---|---|\n| S11 | 85 |",
    kind="table",
):
    db.add(
        Document(
            id=doc_id,
            tenant_id=tenant,
            dept=dept,
            doc_name=f"{doc_id}.pdf",
            minio_object=doc_id,
        )
    )
    db.add(
        Chunk(
            id=f"c-{doc_id}",
            doc_id=doc_id,
            content=text,
            chunk_type=kind,
            chunk_idx=0,
            page_num=3,
        )
    )
    db.add(
        KnowledgeDocumentMetadata(
            doc_id=doc_id, tenant_id=tenant, version_status="active"
        )
    )
    await db.commit()


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-09-14T00:00:00", "2026-09-15T00:00:00Z"),
        ("2026-09-15T00:00:00Z", "2026-09-14T00:00:00Z"),
        ("2026-07-01T00:00:00Z", "2026-09-14T00:00:00Z"),
    ],
)
def test_range_rejects_ambiguous_or_unbounded(start, end):
    with pytest.raises(ValidationError):
        TimeRange(start=start, end=end)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_telemetry(value):
    with pytest.raises(ValidationError):
        ImportRequest(points=[point(value=value)])


@pytest.mark.asyncio
async def test_import_atomic_idempotent_and_tenant(test_db):
    body = ImportRequest(points=[point(), point()])
    assert await telemetry.import_points(test_db, user(), body) == {
        "imported": 1,
        "duplicates": 1,
        "sourceMode": "user_import",
        "controlExecuted": False,
    }
    with pytest.raises(BizError, match="内容不同"):
        await telemetry.import_points(
            test_db,
            user(),
            ImportRequest(
                points=[point(observed_at="2026-09-14T09:00:00+08:00"), point(value=99)]
            ),
        )
    assert (
        await test_db.execute(select(func.count()).select_from(TelemetryPoint))
    ).scalar() == 1
    assert (await telemetry.query(test_db, user(tenant="b"), query()))["stats"] is None


@pytest.mark.asyncio
async def test_telemetry_stats_quality_and_end_exclusive(test_db):
    await telemetry.import_points(
        test_db,
        user(),
        ImportRequest(
            points=[
                point(),
                point(value=30, observed_at="2026-09-14T09:00:00+08:00"),
                point(
                    value=999, quality="bad", observed_at="2026-09-14T10:00:00+08:00"
                ),
                point(value=999, observed_at="2026-09-15T00:00:00Z"),
            ]
        ),
    )
    result = await telemetry.query(test_db, user(), query())
    assert result["stats"]["sampleMean"] == 20
    assert result["stats"]["delta"] == 20
    assert result["excluded"] == 1
    assert len(result["points"]) == 3


@pytest.mark.asyncio
async def test_mixed_units_refused(test_db):
    await telemetry.import_points(
        test_db,
        user(),
        ImportRequest(
            points=[point(), point(unit="K", observed_at="2026-09-14T09:00:00+08:00")]
        ),
    )
    with pytest.raises(BizError, match="混合单位"):
        await telemetry.query(test_db, user(), query())


@pytest.mark.asyncio
async def test_table_exact_evidence_acl_and_tenant(test_db):
    await seed_doc(test_db, dept="ops")
    body = TableQuery(doc_id="d1", column="温度（℃）", filters={"型号": "S11"})
    result = await tables.lookup(test_db, user(role="operator"), body)
    assert result["answer"] == "85"
    assert result["matches"][0]["page"] == 3
    assert result["matches"][0]["rowNumber"] == 2
    for actor in [
        user(tenant="b"),
        user(role="operator", dept=""),
        user(role="operator", dept="other"),
    ]:
        with pytest.raises(BizError):
            await tables.lookup(test_db, actor, body)


@pytest.mark.asyncio
async def test_catalog_respects_tenant_and_document_acl(test_db):
    await seed_doc(test_db, doc_id="visible", dept="ops")
    await seed_doc(test_db, doc_id="other-dept", dept="secret")
    await seed_doc(test_db, doc_id="other-tenant", tenant="b")
    test_db.add_all(
        [
            RealtimeDeviceMapping(
                tenant_id="a",
                source="scada",
                source_device_id="raw-visible",
                canonical_device_id="T1",
                canonical_name="1号主变",
            ),
            RealtimeDeviceMapping(
                tenant_id="b",
                source="scada",
                source_device_id="raw-other",
                canonical_device_id="T2",
                canonical_name="不可见",
            ),
        ]
    )
    await test_db.commit()
    docs = await catalog.documents(test_db, user(role="operator"), "")
    assert [item["id"] for item in docs["list"]] == ["visible"]
    mapped = await catalog.devices(test_db, user(role="operator"), "")
    assert mapped["list"] == [{"id": "T1", "name": "1号主变", "station": ""}]


@pytest.mark.asyncio
async def test_table_duplicate_not_guessed(test_db):
    await seed_doc(test_db, text="| 型号 | 值 |\n|---|---|\n| S11 | 85 |\n| S11 | 90 |")
    result = await tables.lookup(
        test_db, user(), TableQuery(doc_id="d1", column="值", filters={"型号": "S11"})
    )
    assert result["status"] == "needs_review"
    assert len(result["matches"]) == 2


@pytest.mark.parametrize(
    "text", ["没有表格", "| A | A |\n|---|---|\n|1|2|", "|A|B|\n|---|---|\n|1|"]
)
def test_malformed_table(text):
    assert tables.parse_table(text) is None


@pytest.mark.asyncio
async def test_table_withdrawn_refused(test_db):
    await seed_doc(test_db)
    meta = await test_db.get(KnowledgeDocumentMetadata, "d1")
    meta.version_status = "withdrawn"
    await test_db.commit()
    with pytest.raises(BizError, match="非有效版本"):
        await tables.lookup(
            test_db,
            user(),
            TableQuery(doc_id="d1", column="温度（℃）", filters={"型号": "S11"}),
        )


@pytest.mark.asyncio
async def test_impact_snapshot_and_revoked_access(test_db):
    await seed_doc(test_db, "old", text="3.1 油温不得超过85℃", kind="child")
    await seed_doc(test_db, "new", text="3.1 油温不得超过80℃", kind="child")
    result = await impacts.analyze(
        test_db, user(), ImpactRequest(old_doc_id="old", new_doc_id="new")
    )
    assert result["payload"]["changes"][0]["numbersChanged"]
    assert result["payload"]["changes"][0]["before"][0]["chunkId"] == "c-old"
    doc = await test_db.get(Document, "old")
    doc.dept = "secret"
    await test_db.commit()
    with pytest.raises(BizError):
        await snapshots.get_snapshot(test_db, user(role="operator"), result["id"])
    assert (await snapshots.list_snapshots(test_db, user(role="operator"), "impact"))[
        "list"
    ] == []


@pytest.mark.asyncio
async def test_handover_carryover_and_transition(test_db):
    test_db.add(
        Ticket(
            id="t1",
            tenant_id="a",
            title="遗留事项",
            status=TicketStatus.DRAFT,
            created_at=datetime(2026, 9, 1),
        )
    )
    test_db.add(
        Ticket(
            id="t2",
            tenant_id="b",
            title="其他租户",
            status=TicketStatus.DRAFT,
            created_at=datetime(2026, 9, 1),
        )
    )
    await test_db.commit()
    result = await shifts.generate(
        test_db,
        user(),
        HandoverRequest(
            title="白班", start="2026-09-14T00:00:00Z", end="2026-09-14T08:00:00Z"
        ),
    )
    assert [t["id"] for t in result["payload"]["tickets"]] == ["t1"]
    reviewed = await snapshots.review_snapshot(
        test_db,
        user(),
        result["id"],
        ReviewRequest(version=1, action="review", note="已核对"),
    )
    assert reviewed["status"] == "reviewed"
    with pytest.raises(BizError, match="不同"):
        await snapshots.review_snapshot(
            test_db,
            user(),
            result["id"],
            ReviewRequest(version=2, action="accept", note="接班"),
        )
    accepted = await snapshots.review_snapshot(
        test_db,
        user(name="bob"),
        result["id"],
        ReviewRequest(version=2, action="accept", note="接班"),
    )
    assert accepted["version"] == 3
    assert len(accepted["audit"]) == 2
    with pytest.raises(BizError):
        await snapshots.review_snapshot(
            test_db,
            user(name="charlie"),
            result["id"],
            ReviewRequest(version=2, action="accept", note="重复"),
        )
    with pytest.raises(BizError):
        await snapshots.get_snapshot(test_db, user(tenant="b"), result["id"])


@pytest.mark.asyncio
async def test_dossier_requires_canonical_mapping(test_db):
    with pytest.raises(BizError):
        await devices.dossier(test_db, user(), "T1")
    test_db.add(
        RealtimeDeviceMapping(
            tenant_id="a",
            source="scada",
            source_device_id="raw1",
            canonical_device_id="T1",
            canonical_name="1号主变",
        )
    )
    test_db.add(
        RealtimeEvent(
            tenant_id="a",
            source="scada",
            event_id="e1",
            canonical_device_id="T1",
            title="温升",
            occurred_at=datetime(2026, 9, 14),
        )
    )
    test_db.add(
        RealtimeEvent(
            tenant_id="b",
            source="scada",
            event_id="e2",
            canonical_device_id="T1",
            title="不可见",
            occurred_at=datetime(2026, 9, 14),
        )
    )
    await test_db.commit()
    result = await devices.dossier(test_db, user(), "T1")
    assert len(result["timeline"]) == 1
    assert result["timeline"][0]["title"] == "温升"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,path,body,expected",
    [
        (
            "auditor",
            "/handovers",
            {
                "title": "班次",
                "start": "2026-09-14T00:00:00Z",
                "end": "2026-09-14T08:00:00Z",
            },
            403,
        ),
        ("operator", "/impacts", {"old_doc_id": "old", "new_doc_id": "new"}, 403),
        ("editor", "/telemetry/import", {"points": [point()]}, 403),
    ],
)
async def test_api_permissions(test_db, monkeypatch, role, path, body, expected):
    from app.config import settings
    from app.dependencies import get_current_user
    from app.db.session import get_db
    from app.routers.ops_workbench import router

    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(BizError)
    async def handle(_request, exc):
        return JSONResponse({"code": exc.code, "message": exc.message})

    async def session():
        yield test_db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: user(role=role)
    monkeypatch.setattr(settings, "OPS_WORKBENCH_ENABLE", True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/ops-workbench{path}", json=body)
    assert response.status_code == 200
    assert response.json()["code"] == expected
