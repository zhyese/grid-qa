"""持久化任务队列与事件 Outbox 的 SQLite 级集成测试。"""
import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.events import worker as event_worker
from app.events.registry import register_event_handler
from app.events.worker import dispatch_event_once
from app.models.domain_event import DomainEvent, EventDelivery
from app.models.persistent_task import PersistentTask
from app.services import event_center_service, task_queue_service
from app.tasks import worker as task_worker
from app.tasks.registry import register_task_handler
from app.tasks.worker import run_task_once


@pytest_asyncio.fixture
async def task_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        PersistentTask.__table__,
        DomainEvent.__table__,
        EventDelivery.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_tenant_scoped(task_db):
    async with task_db() as db:
        first = await task_queue_service.enqueue_task_record(
            db,
            "test.idempotent",
            {"value": 1},
            tenant_id="tenant-a",
            idempotency_key="same-request",
        )
        duplicate = await task_queue_service.enqueue_task_record(
            db,
            "test.idempotent",
            {"value": 999},
            tenant_id="tenant-a",
            idempotency_key="same-request",
        )
        other_tenant = await task_queue_service.enqueue_task_record(
            db,
            "test.idempotent",
            {"value": 2},
            tenant_id="tenant-b",
            idempotency_key="same-request",
        )

    assert duplicate.id == first.id
    assert duplicate.payload == {"value": 1}
    assert other_tenant.id != first.id


@pytest.mark.asyncio
async def test_worker_retries_then_succeeds(task_db):
    calls = []

    async def flaky(payload, context):
        calls.append(context.attempt)
        if len(calls) == 1:
            raise RuntimeError("temporary")
        return {"accepted": payload["value"]}

    register_task_handler("test.retry_then_success", flaky)
    async with task_db() as db:
        task = await task_queue_service.enqueue_task_record(
            db,
            "test.retry_then_success",
            {"value": 7},
            queue="test-retry",
            max_attempts=3,
        )

    assert await run_task_once(
        queue="test-retry", worker_id="worker-test", session_factory=task_db
    )
    async with task_db() as db:
        failed = await db.get(PersistentTask, task.id)
        assert failed.status == "failed"
        assert failed.attempts == 1
        failed.run_after = task_queue_service.utcnow() - timedelta(seconds=1)
        await db.commit()

    assert await run_task_once(
        queue="test-retry", worker_id="worker-test", session_factory=task_db
    )
    async with task_db() as db:
        succeeded = await db.get(PersistentTask, task.id)
        assert succeeded.status == "succeeded"
        assert succeeded.attempts == 2
        assert succeeded.result == {"accepted": 7}
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_worker_moves_exhausted_task_to_dead(task_db):
    async def always_fails(payload, context):
        raise ValueError("permanent")

    register_task_handler("test.always_dead", always_fails)
    async with task_db() as db:
        task = await task_queue_service.enqueue_task_record(
            db,
            "test.always_dead",
            {},
            queue="test-dead",
            max_attempts=1,
        )
    await run_task_once(
        queue="test-dead", worker_id="worker-dead", session_factory=task_db
    )
    async with task_db() as db:
        dead = await db.get(PersistentTask, task.id)
        assert dead.status == "dead"
        assert "permanent" in dead.last_error
        assert dead.finished_at is not None


@pytest.mark.asyncio
async def test_event_outbox_delivers_to_subscriber(task_db):
    received = []

    async def handler(payload, context):
        received.append((payload, context.event_type, context.tenant_id))

    register_event_handler(
        "test.outbox.created",
        "tests.outbox-success",
        handler,
    )
    async with task_db() as db:
        event = await event_center_service.publish_event_record(
            db,
            "test.outbox.created",
            {"deviceId": "T1"},
            tenant_id="tenant-a",
            source="tests",
            idempotency_key="evt-1",
        )

    assert await dispatch_event_once(worker_id="event-worker", session_factory=task_db)
    async with task_db() as db:
        published = await db.get(DomainEvent, event.id)
        deliveries = await event_center_service.get_event_deliveries(
            db, event.id, tenant_id="tenant-a"
        )
        assert published.status == "published"
        assert [item.status for item in deliveries] == ["succeeded"]
    assert received == [({"deviceId": "T1"}, "test.outbox.created", "tenant-a")]


@pytest.mark.asyncio
async def test_event_retry_skips_already_succeeded_subscriber(task_db):
    calls = {"stable": 0, "flaky": 0}

    async def stable(payload, context):
        calls["stable"] += 1

    async def flaky(payload, context):
        calls["flaky"] += 1
        if calls["flaky"] == 1:
            raise RuntimeError("subscriber unavailable")

    register_event_handler(
        "test.partial.retry", "tests.partial-stable", stable
    )
    register_event_handler(
        "test.partial.retry", "tests.partial-flaky", flaky
    )
    async with task_db() as db:
        event = await event_center_service.publish_event_record(
            db, "test.partial.retry", {"x": 1}, source="tests"
        )

    await dispatch_event_once(worker_id="event-worker", session_factory=task_db)
    async with task_db() as db:
        failed = await db.get(DomainEvent, event.id)
        assert failed.status == "failed"
        failed.available_at = task_queue_service.utcnow() - timedelta(seconds=1)
        deliveries = await event_center_service.get_event_deliveries(
            db, event.id, tenant_id="default"
        )
        for delivery in deliveries:
            if delivery.status == "failed":
                delivery.next_attempt_at = task_queue_service.utcnow() - timedelta(seconds=1)
        await db.commit()

    await dispatch_event_once(worker_id="event-worker", session_factory=task_db)
    async with task_db() as db:
        published = await db.get(DomainEvent, event.id)
        assert published.status == "published"
    assert calls == {"stable": 1, "flaky": 2}


@pytest.mark.asyncio
async def test_event_idempotency_and_no_subscriber_publish(task_db):
    async with task_db() as db:
        first = await event_center_service.publish_event_record(
            db,
            "test.no-subscriber",
            {"v": 1},
            tenant_id="tenant-a",
            source="source-a",
            idempotency_key="external-42",
        )
        duplicate = await event_center_service.publish_event_record(
            db,
            "test.no-subscriber",
            {"v": 2},
            tenant_id="tenant-a",
            source="source-a",
            idempotency_key="external-42",
        )
    assert duplicate.id == first.id

    await dispatch_event_once(worker_id="event-worker", session_factory=task_db)
    async with task_db() as db:
        published = await db.get(DomainEvent, first.id)
        assert published.status == "published"
        assert published.payload == {"v": 1}


@pytest.mark.asyncio
async def test_task_worker_loop_survives_transient_iteration_error(
    task_db, monkeypatch
):
    stop_event = asyncio.Event()
    calls = 0
    degraded_tags = []

    async def no_stale_recovery(db, *, stale_after_seconds):
        return 0

    async def transient_run(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("db connection reset")
        stop_event.set()
        return False

    monkeypatch.setattr(
        task_queue_service, "recover_stale_tasks", no_stale_recovery
    )
    monkeypatch.setattr(task_worker, "run_task_once", transient_run)
    monkeypatch.setattr(
        task_worker,
        "degraded",
        lambda tag, exc, msg="": degraded_tags.append((tag, str(exc), msg)),
    )

    await asyncio.wait_for(
        task_worker.task_worker_loop(
            queue="resilient",
            stop_event=stop_event,
            poll_interval=0.01,
            session_factory=task_db,
        ),
        timeout=1,
    )

    assert calls == 2
    assert degraded_tags[0][0] == "task_worker_loop"
    assert "db connection reset" in degraded_tags[0][1]


@pytest.mark.asyncio
async def test_event_dispatcher_loop_survives_transient_iteration_error(
    task_db, monkeypatch
):
    stop_event = asyncio.Event()
    calls = 0
    degraded_tags = []

    async def no_stale_recovery(db, *, stale_after_seconds):
        return 0

    async def transient_dispatch(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("event db unavailable")
        stop_event.set()
        return False

    monkeypatch.setattr(
        event_center_service, "recover_stale_events", no_stale_recovery
    )
    monkeypatch.setattr(event_worker, "dispatch_event_once", transient_dispatch)
    monkeypatch.setattr(
        event_worker,
        "degraded",
        lambda tag, exc, msg="": degraded_tags.append((tag, str(exc), msg)),
    )

    await asyncio.wait_for(
        event_worker.event_dispatcher_loop(
            stop_event=stop_event,
            poll_interval=0.01,
            session_factory=task_db,
        ),
        timeout=1,
    )

    assert calls == 2
    assert degraded_tags[0][0] == "event_dispatcher_loop"
    assert "event db unavailable" in degraded_tags[0][1]


@pytest.mark.asyncio
async def test_long_event_subscriber_renews_dispatch_lease(task_db):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(payload, context):
        started.set()
        await release.wait()

    register_event_handler(
        "test.long-subscriber",
        "tests.long-subscriber-heartbeat",
        slow_handler,
    )
    async with task_db() as db:
        event = await event_center_service.publish_event_record(
            db,
            "test.long-subscriber",
            {"deviceId": "T-slow"},
            source="tests",
        )

    dispatch = asyncio.create_task(
        dispatch_event_once(
            worker_id="event-worker-heartbeat",
            session_factory=task_db,
            heartbeat_interval=0.01,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        async with task_db() as db:
            claimed = await db.get(DomainEvent, event.id)
            first_locked_at = claimed.locked_at

        await asyncio.sleep(0.16)

        async with task_db() as db:
            renewed = await db.get(DomainEvent, event.id)
            assert renewed.status == "dispatching"
            assert renewed.locked_by == "event-worker-heartbeat"
            assert renewed.locked_at is not None
            assert renewed.locked_at > first_locked_at
    finally:
        release.set()

    assert await asyncio.wait_for(dispatch, timeout=1)
    async with task_db() as db:
        published = await db.get(DomainEvent, event.id)
        assert published.status == "published"
        assert published.locked_by == ""
        assert published.locked_at is None
