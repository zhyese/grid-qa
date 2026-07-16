"""旧 system 告警处置链路的租户隔离与持久化任务测试。"""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.alert_disposal import AlertDisposal
from app.models.persistent_task import PersistentTask
from app.models.ticket import Ticket, TicketStatus
from app.services import alert_disposal_service as service


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AlertDisposal.__table__.create)
        await conn.run_sync(PersistentTask.__table__.create)
        await conn.run_sync(Ticket.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_trigger_disposal_persists_tenant_scoped_task(
    session_factory, monkeypatch,
):
    monkeypatch.setattr(service, "AsyncSessionLocal", session_factory)

    disposal_id = await service.trigger_disposal(
        "critical", "主变温度越限", "顶层油温 95℃", tenant_id="tenant-a",
    )

    async with session_factory() as db:
        disposal = await db.get(AlertDisposal, disposal_id)
        task = (await db.execute(select(PersistentTask))).scalar_one()
    assert disposal.tenant_id == "tenant-a"
    assert disposal.status == "pending"
    assert task.tenant_id == "tenant-a"
    assert task.task_type == service.TASK_TYPE
    assert task.payload["disposal_id"] == disposal_id
    assert task.queue_name == "realtime"


@pytest.mark.asyncio
async def test_list_disposals_only_returns_current_tenant(
    session_factory, monkeypatch,
):
    monkeypatch.setattr(service, "AsyncSessionLocal", session_factory)
    async with session_factory() as db:
        db.add_all([
            AlertDisposal(tenant_id="tenant-a", title="A", status="proposed"),
            AlertDisposal(tenant_id="tenant-b", title="B", status="proposed"),
        ])
        await db.commit()

    data = await service.list_disposals(tenant_id="tenant-a")

    assert data["total"] == 1
    assert [row["title"] for row in data["list"]] == ["A"]
    assert data["list"][0]["tenantId"] == "tenant-a"


@pytest.mark.asyncio
async def test_review_actions_cannot_mutate_another_tenant(session_factory):
    async with session_factory() as db:
        row = AlertDisposal(
            tenant_id="tenant-a", title="A", status="proposed",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        row_id = row.id

        with pytest.raises(ValueError, match="处置记录不存在"):
            await service.confirm_disposal(
                db, row_id, reviewer="tenant-b-admin", tenant_id="tenant-b",
            )
        await db.rollback()

        unchanged = await db.get(AlertDisposal, row_id)
        assert unchanged.status == "proposed"
        confirmed = await service.confirm_disposal(
            db, row_id, reviewer="tenant-a-admin", tenant_id="tenant-a",
        )
        assert confirmed["status"] == "confirmed"
        assert confirmed["tenantId"] == "tenant-a"


@pytest.mark.asyncio
async def test_to_ticket_is_tenant_scoped_and_creates_draft_only(session_factory):
    async with session_factory() as db:
        row = AlertDisposal(
            tenant_id="tenant-a",
            title="主变检查",
            status="confirmed",
            ticket_draft_json='{"steps":["核对设备"],"safety":["专人监护"]}',
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        row_id = row.id

        with pytest.raises(ValueError, match="处置记录不存在"):
            await service.to_ticket(
                db, row_id, creator="tenant-b-admin", tenant="tenant-b",
            )
        await db.rollback()

        result = await service.to_ticket(
            db, row_id, creator="tenant-a-admin", tenant="tenant-a",
        )
        ticket = (await db.execute(select(Ticket))).scalar_one()
        assert result["disposal"]["status"] == "ticketed"
        assert ticket.tenant_id == "tenant-a"
        assert ticket.status is TicketStatus.DRAFT
        assert ticket.source_ref == f"alert-disposal:{row_id}"


def test_task_handler_uses_worker_tenant_context(monkeypatch):
    captured = {}

    async def fake_run(disp_id, alert_text, model_type, tenant_id="default"):
        captured.update(
            id=disp_id, text=alert_text, model=model_type, tenant=tenant_id,
        )
        return {"status": "proposed"}

    monkeypatch.setattr(service, "_run_disposal", fake_run)
    context = SimpleNamespace(tenant_id="tenant-a")

    import asyncio
    result = asyncio.run(service.alert_disposal_task_handler(
        {"disposal_id": 7, "alert_text": "alarm", "model_type": "deepseek"},
        context,
    ))

    assert result == {"status": "proposed"}
    assert captured == {
        "id": 7, "text": "alarm", "model": "deepseek", "tenant": "tenant-a",
    }


@pytest.mark.asyncio
async def test_system_routes_forward_current_user_tenant(monkeypatch):
    from app.routers import system

    captured = {}

    async def fake_trigger(*args, **kwargs):
        captured["trigger"] = kwargs
        return 9

    async def fake_list(*args, **kwargs):
        captured["list"] = kwargs
        return {"total": 0, "list": []}

    async def fake_log(*args, **kwargs):
        return None

    monkeypatch.setattr(system, "trigger_disposal", fake_trigger)
    monkeypatch.setattr(system, "list_disposals", fake_list)
    monkeypatch.setattr(system, "write_log", fake_log)
    user = SimpleNamespace(username="admin-a", tenant_id="tenant-a")
    body = SimpleNamespace(
        severity="warning", title="alarm", summary="summary", modelType=None,
    )

    await system.alerts_dispose(body=body, db=object(), admin=user)
    await system.alerts_disposals(
        page=1, size=20, status=None, user=user,
    )

    assert captured["trigger"]["tenant_id"] == "tenant-a"
    assert captured["list"]["tenant_id"] == "tenant-a"
