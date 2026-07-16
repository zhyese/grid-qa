"""实时事件、持久任务、知识治理与两票闭环的轻量集成测试。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.alert_disposal import AlertDisposal
from app.models.document import Document
from app.models.domain_event import DomainEvent
from app.models.knowledge_governance import KnowledgeDocumentMetadata
from app.models.persistent_task import PersistentTask
from app.models.realtime_event import (
    ProactiveOpsRun,
    RealtimeDeviceMapping,
    RealtimeEvent,
)
from app.models.ticket import Ticket, TicketStatus, TicketType
from app.schemas.realtime_event import RealtimeEventIn
from app.services import (
    qa_service,
    realtime_event_service,
    task_queue_service,
    ticket_lifecycle_service,
)
from app.models.qa_cache import QaCache
from app.rag import semantic_cache
from app.tasks.worker import run_task_once


@pytest_asyncio.fixture
async def active_ops_db():
    """单连接内存库足以覆盖事务与 worker 多 session 接力。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    tables = [
        Document.__table__,
        KnowledgeDocumentMetadata.__table__,
        RealtimeDeviceMapping.__table__,
        RealtimeEvent.__table__,
        ProactiveOpsRun.__table__,
        AlertDisposal.__table__,
        PersistentTask.__table__,
        DomainEvent.__table__,
        Ticket.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _event_body(event_id: str) -> RealtimeEventIn:
    return RealtimeEventIn(
        eventId=event_id,
        source="scada",
        eventType="alarm",
        severity="critical",
        occurredAt=datetime(2026, 7, 16, 10, 30),
        title="1号主变油温越限",
        summary="顶层油温 96℃",
        payload={"deviceId": "T1", "measurements": {"oilTemperature": 96}},
    )


def _stored_event(event_id: str, *, status: str = "queued") -> RealtimeEvent:
    return RealtimeEvent(
        id=f"db-{event_id}",
        tenant_id="tenant-a",
        event_id=event_id,
        source="scada",
        event_type="alarm",
        severity="critical",
        title="主变油温越限",
        summary="需要只读诊断",
        source_device_id="T1",
        canonical_device_id="SUB-A:T1",
        canonical_device_name="1号主变",
        device_type="main_transformer",
        station="A站",
        device_mapped=True,
        occurred_at=datetime(2026, 7, 16, 10, 30),
        last_received_at=datetime(2026, 7, 16, 10, 30),
        payload_json="{}",
        normalized_json="{}",
        processing_status=status,
        rule_decision="trigger",
        rule_reason="critical alarm",
    )


@pytest.mark.asyncio
async def test_ingest_atomically_persists_event_run_task_and_outbox(active_ops_db):
    async with active_ops_db() as db:
        result = await realtime_event_service.ingest_event(
            db,
            _event_body("SCADA-ATOMIC-1"),
            tenant_id="tenant-a",
            actor="connector-a",
        )

    async with active_ops_db() as db:
        event = (await db.execute(select(RealtimeEvent))).scalar_one()
        run = (await db.execute(select(ProactiveOpsRun))).scalar_one()
        task = (await db.execute(select(PersistentTask))).scalar_one()
        outbox = (await db.execute(select(DomainEvent))).scalar_one()

    assert result["duplicate"] is False
    assert run.event_ref_id == event.id
    assert run.task_id == task.id == result["queue"]["taskId"]
    assert task.payload == {"run_id": run.id}
    assert task.idempotency_key == f"proactive:{run.id}"
    assert outbox.event_type == realtime_event_service.NORMALIZED_EVENT_TYPE
    assert outbox.aggregate_id == event.id
    assert {event.tenant_id, run.tenant_id, task.tenant_id, outbox.tenant_id} == {
        "tenant-a"
    }


@pytest.mark.asyncio
async def test_ingest_rolls_back_every_record_when_task_enqueue_fails(
    active_ops_db, monkeypatch
):
    async def fail_enqueue(*args, **kwargs):
        raise RuntimeError("queue unavailable before transaction commit")

    monkeypatch.setattr(task_queue_service, "enqueue_task_record", fail_enqueue)

    with pytest.raises(RuntimeError, match="queue unavailable"):
        async with active_ops_db() as db:
            await realtime_event_service.ingest_event(
                db,
                _event_body("SCADA-ATOMIC-ROLLBACK"),
                tenant_id="tenant-a",
            )

    async with active_ops_db() as db:
        for model in (RealtimeEvent, ProactiveOpsRun, PersistentTask, DomainEvent):
            count = (
                await db.execute(select(func.count()).select_from(model))
            ).scalar_one()
            assert count == 0


@pytest.mark.asyncio
async def test_restarted_worker_takes_over_running_proactive_run(
    active_ops_db, monkeypatch
):
    event = _stored_event("SCADA-TAKEOVER", status="processing")
    run = ProactiveOpsRun(
        id="run-takeover",
        tenant_id="tenant-a",
        event_ref_id=event.id,
        triggered_by="connector-a",
        status="running",
        risk_level="critical",
        execution_mode="read_only",
        requires_human_review=True,
        control_executed=False,
        started_at=datetime.now() - timedelta(minutes=10),
    )
    stale_at = task_queue_service.utcnow() - timedelta(minutes=10)
    async with active_ops_db() as db:
        db.add_all([event, run])
        await db.flush()
        task = await task_queue_service.enqueue_task_record(
            db,
            realtime_event_service.TASK_TYPE,
            {"run_id": run.id},
            queue="realtime",
            idempotency_key=f"proactive:{run.id}",
            tenant_id="tenant-a",
            max_attempts=3,
            commit=False,
        )
        run.task_id = task.id
        task.status = "running"
        task.attempts = 1
        task.locked_by = "dead-worker"
        task.locked_at = stale_at
        task.heartbeat_at = stale_at
        await db.commit()

    async with active_ops_db() as db:
        assert await task_queue_service.recover_stale_tasks(
            db, stale_after_seconds=60
        ) == 1

    async def fake_get_persona(name):
        assert name == "alert"
        return SimpleNamespace(
            name="ALERT_PERSONA",
            allowed_tools=[
                "search_regulation",
                "query_equipment_graph",
                "search_similar_case",
                "draft_ticket",
            ],
        )

    async def fake_run_agent(db, persona, prompt, model_type, ctx):
        assert ctx["tenant"] == "tenant-a"
        assert ctx["username"] == "tenant-a:connector-a"
        assert set(persona.allowed_tools) <= {
            "search_regulation", "query_equipment_graph", "search_similar_case",
        }
        assert "draft_ticket" not in persona.allowed_tools
        assert "禁止执行遥控" in prompt
        return SimpleNamespace(
            answer={
                "summary": "检查冷却系统并持续监测",
                "diagnosis": "油温异常",
                "handling": "由值班人员现场核验",
                "risks": ["温升持续"],
                "ticket": {"steps": ["核对设备", "检查冷却器"]},
            },
            steps=[{"tool": "knowledge_search"}],
            tools_used=["knowledge_search"],
            iterations=1,
            degraded=False,
            degrade_reason="",
            latency_ms=5,
        )

    from app.db import session as session_module
    from app.services import agent_runtime, persona_store

    monkeypatch.setattr(session_module, "AsyncSessionLocal", active_ops_db)
    monkeypatch.setattr(persona_store, "get_persona", fake_get_persona)
    monkeypatch.setattr(agent_runtime, "run_agent", fake_run_agent)

    assert await run_task_once(
        queue="realtime",
        worker_id="replacement-worker",
        session_factory=active_ops_db,
    )

    async with active_ops_db() as db:
        recovered_task = await db.get(PersistentTask, task.id)
        recovered_run = await db.get(ProactiveOpsRun, run.id)
        recovered_event = await db.get(RealtimeEvent, event.id)
        proposal = (
            await db.execute(
                select(DomainEvent).where(
                    DomainEvent.event_type
                    == realtime_event_service.PROPOSAL_EVENT_TYPE
                )
            )
        ).scalar_one()

    assert recovered_task.status == "succeeded"
    assert recovered_task.attempts == 2
    assert recovered_run.status == "proposed"
    assert recovered_run.control_executed is False
    assert recovered_event.processing_status == "completed"
    assert proposal.aggregate_id == run.id


@pytest.mark.asyncio
async def test_retry_run_creates_new_generation_instead_of_reusing_dead_task(
    active_ops_db, monkeypatch
):
    event = _stored_event("SCADA-RETRY", status="failed")
    run = ProactiveOpsRun(
        id="run-retry",
        tenant_id="tenant-a",
        event_ref_id=event.id,
        triggered_by="connector-a",
        status="failed",
        risk_level="critical",
        error_message="previous worker exhausted retries",
        execution_mode="read_only",
        requires_human_review=True,
        control_executed=False,
    )
    async with active_ops_db() as db:
        db.add_all([event, run])
        await db.flush()
        dead_task = await task_queue_service.enqueue_task_record(
            db,
            realtime_event_service.TASK_TYPE,
            {"run_id": run.id},
            queue="realtime",
            idempotency_key=f"proactive:{run.id}",
            tenant_id="tenant-a",
            max_attempts=3,
            commit=False,
        )
        dead_task.status = "dead"
        dead_task.attempts = 3
        dead_task.finished_at = task_queue_service.utcnow()
        run.task_id = dead_task.id
        await db.commit()

        monkeypatch.setattr(realtime_event_service.time, "time_ns", lambda: 4242)
        result = await realtime_event_service.retry_run(
            db, run.id, tenant_id="tenant-a", model_type="deepseek"
        )

    async with active_ops_db() as db:
        tasks = (
            await db.execute(select(PersistentTask).order_by(PersistentTask.created_at))
        ).scalars().all()
        retried_run = await db.get(ProactiveOpsRun, run.id)
        old_task = await db.get(PersistentTask, dead_task.id)

    assert len(tasks) == 2
    new_task = next(item for item in tasks if item.id != dead_task.id)
    assert old_task.status == "dead"
    assert new_task.status == "queued"
    assert new_task.idempotency_key == "proactive:run-retry:retry:4242"
    assert retried_run.task_id == new_task.id == result["queue"]["taskId"]
    assert retried_run.status == "queued"


@pytest.mark.asyncio
async def test_qa_cache_gate_rejects_governance_blocked_source(active_ops_db):
    async with active_ops_db() as db:
        db.add(
            Document(
                id="doc-withdrawn",
                doc_name="已撤回规程.pdf",
                minio_object="tenant-a/doc-withdrawn.pdf",
                tenant_id="tenant-a",
            )
        )
        db.add(
            KnowledgeDocumentMetadata(
                doc_id="doc-withdrawn",
                tenant_id="tenant-a",
                owner="ops",
                effective_at=datetime(2025, 1, 1),
                is_permanent=True,
                version_label="V1",
                version_status="withdrawn",
            )
        )
        await db.commit()

        cached = {
            "answer": "旧规程答案",
            "retrievalSource": [{"docId": "doc-withdrawn", "chunk": "..."}],
        }
        assert await qa_service._cache_knowledge_valid(
            db, cached, "tenant-a"
        ) is False
        # 带租户的生产缓存必须证明来源文档仍属于当前租户。
        assert await qa_service._cache_knowledge_valid(
            db, cached, "tenant-b"
        ) is False
        assert await qa_service._cache_knowledge_valid(
            db, {"answer": "no sources", "retrievalSource": []}, "tenant-a"
        ) is False


def test_qa_cache_keys_are_tenant_scoped():
    assert qa_service._cache_key("deepseek", "油温高", "tenant-a") != qa_service._cache_key(
        "deepseek", "油温高", "tenant-b"
    )
    assert QaCache.build_hash("deepseek", "油温高", "tenant-a") != QaCache.build_hash(
        "deepseek", "油温高", "tenant-b"
    )
    assert semantic_cache._semantic_index_key("tenant-a") != semantic_cache._semantic_index_key("tenant-b")


@pytest.mark.asyncio
async def test_proactive_ticket_source_ref_is_unique_and_idempotent(active_ops_db):
    constraint_names = {item.name for item in Ticket.__table__.constraints}
    assert "uq_tickets_tenant_source_ref" in constraint_names

    event = _stored_event("SCADA-TICKET", status="completed")
    run = ProactiveOpsRun(
        id="run-ticket",
        tenant_id="tenant-a",
        event_ref_id=event.id,
        triggered_by="connector-a",
        status="confirmed",
        risk_level="critical",
        ticket_draft_json=json.dumps(
            {
                "ticketType": "操作票",
                "task": "核验主变冷却系统",
                "device": "1号主变",
                "steps": ["核对设备", "检查冷却器"],
                "safety": ["严禁遥控操作"],
            },
            ensure_ascii=False,
        ),
        execution_mode="read_only",
        requires_human_review=True,
        control_executed=False,
    )
    async with active_ops_db() as db:
        db.add_all([event, run])
        await db.commit()
        created = await realtime_event_service.run_to_ticket(
            db, run.id, tenant_id="tenant-a", creator="operator-a"
        )
        duplicate = await ticket_lifecycle_service.create_ticket(
            db,
            ticket_type="操作票",
            task="网络重试不应新建票据",
            creator="operator-a",
            tenant="tenant-a",
            source_ref=f"proactive:{run.id}",
        )
        count = (
            await db.execute(
                select(func.count(Ticket.id)).where(Ticket.tenant_id == "tenant-a")
            )
        ).scalar_one()

    assert created["ticket"]["sourceRef"] == "proactive:run-ticket"
    assert duplicate["id"] == created["ticket"]["id"]
    assert duplicate["task"] == "核验主变冷却系统"
    assert count == 1
    assert created["run"]["status"] == "ticketed"
    assert created["run"]["controlExecuted"] is False


@pytest.mark.asyncio
async def test_proactive_handler_ignores_stale_task_generation(active_ops_db):
    event = _stored_event("SCADA-STALE-GENERATION", status="queued")
    run = ProactiveOpsRun(
        id="run-stale-generation",
        tenant_id="tenant-a",
        event_ref_id=event.id,
        triggered_by="connector-a",
        status="queued",
        risk_level="critical",
        task_id="current-task",
        execution_mode="read_only",
        requires_human_review=True,
        control_executed=False,
    )
    async with active_ops_db() as db:
        db.add_all([event, run])
        await db.commit()

        result = await realtime_event_service.process_proactive_run(
            db,
            run.id,
            tenant_id="tenant-a",
            expected_task_id="old-task",
        )
        await db.refresh(run)

    assert result["ignored"] is True
    assert result["reason"] == "stale_task_generation"
    assert run.status == "queued"
    assert run.task_id == "current-task"


@pytest.mark.asyncio
async def test_ticket_lifecycle_single_ticket_actions_are_tenant_bound(active_ops_db):
    async with active_ops_db() as db:
        ticket = Ticket(
            id="ticket-tenant-a",
            tenant_id="tenant-a",
            ticket_type=TicketType.OPERATION,
            status=TicketStatus.REVIEWED,
            title="主变检查",
            task="核验主变冷却系统",
            creator="operator-a",
        )
        db.add(ticket)
        await db.commit()
        ticket_id = ticket.id

        assert await ticket_lifecycle_service.get_ticket(
            db, ticket_id, tenant="tenant-b",
        ) is None
        with pytest.raises(ValueError, match="票据不存在"):
            await ticket_lifecycle_service.issue_ticket(
                db, ticket_id, issuer="tenant-b-user", tenant="tenant-b",
            )
        await db.rollback()

        issued = await ticket_lifecycle_service.issue_ticket(
            db, ticket_id, issuer="tenant-a-user", tenant="tenant-a",
        )
        assert issued["status"] == TicketStatus.ISSUED.value

        assert await ticket_lifecycle_service.delete_ticket(
            db, ticket_id, tenant="tenant-b",
        ) is False
        assert await ticket_lifecycle_service.delete_ticket(
            db, ticket_id, tenant="tenant-a",
        ) is True
