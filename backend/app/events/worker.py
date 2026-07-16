"""领域事件 Outbox dispatcher。"""
from __future__ import annotations

import asyncio
import inspect
import socket
import traceback
import uuid
from collections.abc import Callable

from sqlalchemy import update

from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.events.registry import EventContext, matching_subscriptions
from app.models.domain_event import DomainEvent
from app.services import event_center_service


async def _invoke(handler, payload: dict, context: EventContext):
    try:
        parameter_count = len(inspect.signature(handler).parameters)
    except (TypeError, ValueError):
        parameter_count = 2
    result = handler(payload) if parameter_count == 1 else handler(payload, context)
    if inspect.isawaitable(result):
        return await result
    return result


async def _heartbeat_loop(
    event_id: str,
    worker_id: str,
    stop_event: asyncio.Event,
    session_factory: Callable,
    interval: float = 30.0,
) -> None:
    """长订阅处理期间续租事件。

    stale 回收以 DomainEvent.locked_at 为准；带 worker 所有权条件的
    UPDATE 可避免与 finalize 并发时把已完成事件重新写成有租约状态。
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=max(0.05, interval)
            )
            break
        except TimeoutError:
            pass
        try:
            async with session_factory() as db:
                result = await db.execute(
                    update(DomainEvent)
                    .where(
                        DomainEvent.id == event_id,
                        DomainEvent.status == "dispatching",
                        DomainEvent.locked_by == worker_id,
                    )
                    .values(locked_at=event_center_service.utcnow())
                )
                await db.commit()
            if result.rowcount != 1:
                break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            degraded(
                "event_dispatcher_heartbeat",
                exc,
                f"event={event_id} worker={worker_id}",
            )


async def dispatch_event_once(
    *,
    worker_id: str | None = None,
    session_factory: Callable = AsyncSessionLocal,
    heartbeat_interval: float = 30.0,
) -> bool:
    worker_id = worker_id or f"{socket.gethostname()}:events:{uuid.uuid4().hex[:8]}"
    async with session_factory() as db:
        event = await event_center_service.claim_next_event(db, worker_id=worker_id)
    if not event:
        return False

    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat_loop(
            event.id,
            worker_id,
            heartbeat_stop,
            session_factory,
            interval=heartbeat_interval,
        ),
        name=f"event-heartbeat-{event.id}",
    )
    try:
        subscriptions = matching_subscriptions(event.event_type)
        async with session_factory() as db:
            await event_center_service.ensure_deliveries(db, event, subscriptions)

        errors: list[str] = []
        for sub in subscriptions:
            async with session_factory() as db:
                delivery = await event_center_service.start_delivery(
                    db, event.id, sub.subscriber
                )
            if not delivery or delivery.status in ("succeeded", "dead"):
                continue
            context = EventContext(
                event_id=event.id,
                event_type=event.event_type,
                tenant_id=event.tenant_id,
                source=event.source,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
                occurred_at=event.occurred_at,
                delivery_attempt=delivery.attempts,
            )
            try:
                await _invoke(sub.handler, event.payload or {}, context)
                async with session_factory() as db:
                    await event_center_service.complete_delivery(db, delivery.id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )
                errors.append(f"{sub.subscriber}: {detail}")
                async with session_factory() as db:
                    await event_center_service.fail_delivery(
                        db, delivery.id, error=detail
                    )

        async with session_factory() as db:
            await event_center_service.finalize_event(
                db, event.id, error="\n".join(errors)
            )
    finally:
        heartbeat_stop.set()
        await asyncio.gather(heartbeat, return_exceptions=True)
    return True


async def event_dispatcher_loop(
    *,
    stop_event: asyncio.Event,
    poll_interval: float = 0.5,
    stale_after_seconds: int = 300,
    session_factory: Callable = AsyncSessionLocal,
) -> None:
    worker_id = f"{socket.gethostname()}:events:{uuid.uuid4().hex[:8]}"
    last_recovery = 0.0
    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        worked = False
        try:
            now = loop.time()
            if now - last_recovery >= max(30.0, stale_after_seconds / 2):
                async with session_factory() as db:
                    await event_center_service.recover_stale_events(
                        db, stale_after_seconds=stale_after_seconds
                    )
                last_recovery = now
            worked = await dispatch_event_once(
                worker_id=worker_id, session_factory=session_factory
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            degraded(
                "event_dispatcher_loop",
                exc,
                f"worker={worker_id}",
            )
        if worked:
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, poll_interval))
        except TimeoutError:
            pass
