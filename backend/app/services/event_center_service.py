"""领域事件 Outbox 的存储、投递状态与查询服务。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.registry import EventSubscription
from app.models.domain_event import DomainEvent, EventDelivery
from app.services.task_queue_service import json_safe, utcnow


async def publish_event(
    event_type: str,
    payload: dict,
    *,
    source: str = "",
    aggregate_type: str = "",
    aggregate_id: str = "",
    tenant_id: str = "default",
    idempotency_key: str = "",
    **kwargs,
) -> str:
    """兼容业务模块的公开发布入口；实际 session 生命周期由 registry 管理。"""
    from app.events.registry import publish_event as _publish

    return await _publish(
        event_type,
        payload,
        source=source,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        **kwargs,
    )


async def publish_event_record(
    db: AsyncSession,
    event_type: str,
    payload: dict,
    *,
    source: str = "",
    aggregate_type: str = "",
    aggregate_id: str = "",
    tenant_id: str = "default",
    idempotency_key: str = "",
    headers: dict | None = None,
    correlation_id: str = "",
    causation_id: str = "",
    schema_version: int = 1,
    max_attempts: int = 5,
    available_at: datetime | None = None,
    commit: bool = True,
) -> DomainEvent:
    """写入事件 Outbox；``commit=False`` 支持与业务数据原子提交。"""
    event_type = event_type.strip()
    if not event_type:
        raise ValueError("event_type 不能为空")
    tenant_id = tenant_id.strip() or "default"
    source = source.strip() or "internal"
    idem = idempotency_key.strip() or None
    if idem:
        existing = (
            await db.execute(
                select(DomainEvent).where(
                    DomainEvent.tenant_id == tenant_id,
                    DomainEvent.source == source,
                    DomainEvent.event_type == event_type,
                    DomainEvent.idempotency_key == idem,
                )
            )
        ).scalar_one_or_none()
        if existing:
            return existing

    event = DomainEvent(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        event_type=event_type,
        source=source,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=json_safe(payload or {}),
        headers=json_safe(headers or {}),
        schema_version=max(1, int(schema_version)),
        status="pending",
        attempts=0,
        max_attempts=max(1, min(100, int(max_attempts))),
        available_at=available_at or utcnow(),
        idempotency_key=idem,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )
    try:
        # Outbox 幂等竞争只回滚保存点，保留调用方事务中的业务数据。
        async with db.begin_nested():
            db.add(event)
            await db.flush()
        if commit:
            await db.commit()
            await db.refresh(event)
        return event
    except IntegrityError:
        if not idem:
            raise
        return (
            await db.execute(
                select(DomainEvent).where(
                    DomainEvent.tenant_id == tenant_id,
                    DomainEvent.source == source,
                    DomainEvent.event_type == event_type,
                    DomainEvent.idempotency_key == idem,
                )
            )
        ).scalar_one()


async def claim_next_event(
    db: AsyncSession, *, worker_id: str
) -> DomainEvent | None:
    now = utcnow()
    event = (
        await db.execute(
            select(DomainEvent)
            .where(
                DomainEvent.status.in_(("pending", "failed")),
                DomainEvent.available_at <= now,
                DomainEvent.attempts < DomainEvent.max_attempts,
            )
            .order_by(DomainEvent.occurred_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if not event:
        await db.rollback()
        return None
    event.status = "dispatching"
    event.attempts += 1
    event.locked_by = worker_id
    event.locked_at = now
    event.last_error = ""
    await db.commit()
    await db.refresh(event)
    return event


async def recover_stale_events(
    db: AsyncSession, *, stale_after_seconds: int = 300
) -> int:
    """回收 dispatcher 异常退出后遗留的 dispatching/running 状态。"""
    cutoff = utcnow() - timedelta(seconds=max(1, stale_after_seconds))
    events = list(
        (
            await db.execute(
                select(DomainEvent)
                .where(
                    DomainEvent.status == "dispatching",
                    or_(DomainEvent.locked_at < cutoff, DomainEvent.locked_at.is_(None)),
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
    )
    if not events:
        await db.rollback()
        return 0
    event_ids = [event.id for event in events]
    deliveries = list(
        (
            await db.execute(
                select(EventDelivery).where(
                    EventDelivery.event_id.in_(event_ids),
                    EventDelivery.status == "running",
                )
            )
        ).scalars().all()
    )
    for delivery in deliveries:
        delivery.last_error = "dispatcher 心跳超时，投递已回收"
        if delivery.attempts >= delivery.max_attempts:
            delivery.status = "dead"
            delivery.finished_at = utcnow()
        else:
            delivery.status = "failed"
            delivery.next_attempt_at = utcnow()
    for event in events:
        event.locked_by = ""
        event.locked_at = None
        event.last_error = "dispatcher 心跳超时，事件已回收"
        if event.attempts >= event.max_attempts:
            event.status = "dead"
        else:
            event.status = "failed"
            event.available_at = utcnow()
    await db.commit()
    return len(events)


async def ensure_deliveries(
    db: AsyncSession,
    event: DomainEvent,
    subscriptions: list[EventSubscription],
) -> dict[str, EventDelivery]:
    existing = {
        row.subscriber: row
        for row in (
            await db.execute(
                select(EventDelivery).where(EventDelivery.event_id == event.id)
            )
        ).scalars().all()
    }
    for sub in subscriptions:
        if sub.subscriber not in existing:
            row = EventDelivery(
                id=str(uuid.uuid4()),
                event_id=event.id,
                tenant_id=event.tenant_id,
                subscriber=sub.subscriber,
                pattern=sub.pattern,
                status="pending",
                attempts=0,
                max_attempts=sub.max_attempts,
                next_attempt_at=utcnow(),
            )
            db.add(row)
            existing[sub.subscriber] = row
    await db.commit()
    return existing


async def start_delivery(
    db: AsyncSession, event_id: str, subscriber: str
) -> EventDelivery | None:
    row = (
        await db.execute(
            select(EventDelivery)
            .where(
                EventDelivery.event_id == event_id,
                EventDelivery.subscriber == subscriber,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not row or row.status in ("succeeded", "dead"):
        await db.commit()
        if row:
            await db.refresh(row)
        return row
    if row.next_attempt_at > utcnow():
        await db.commit()
        return None
    row.status = "running"
    row.attempts += 1
    await db.commit()
    await db.refresh(row)
    return row


async def complete_delivery(
    db: AsyncSession, delivery_id: str
) -> None:
    row = await db.get(EventDelivery, delivery_id)
    if row and row.status == "running":
        row.status = "succeeded"
        row.finished_at = utcnow()
        row.last_error = ""
        await db.commit()


async def fail_delivery(
    db: AsyncSession, delivery_id: str, *, error: str
) -> str | None:
    row = await db.get(EventDelivery, delivery_id)
    if not row or row.status != "running":
        return None
    row.last_error = error[-8000:]
    if row.attempts >= row.max_attempts:
        row.status = "dead"
        row.finished_at = utcnow()
    else:
        row.status = "failed"
        row.next_attempt_at = utcnow() + timedelta(
            seconds=min(300, 2 ** max(0, row.attempts - 1))
        )
    await db.commit()
    return row.status


async def finalize_event(
    db: AsyncSession, event_id: str, *, error: str = ""
) -> str | None:
    event = await db.get(DomainEvent, event_id)
    if not event or event.status != "dispatching":
        return None
    statuses = list(
        (
            await db.execute(
                select(EventDelivery.status).where(EventDelivery.event_id == event_id)
            )
        ).scalars().all()
    )
    event.locked_by = ""
    event.locked_at = None
    event.last_error = error[-8000:]
    if not statuses or all(status == "succeeded" for status in statuses):
        event.status = "published"
        event.published_at = utcnow()
        event.last_error = ""
    elif "dead" in statuses or event.attempts >= event.max_attempts:
        event.status = "dead"
    else:
        event.status = "failed"
        event.available_at = utcnow() + timedelta(
            seconds=min(300, 2 ** max(0, event.attempts - 1))
        )
    await db.commit()
    return event.status


async def retry_event(
    db: AsyncSession, event_id: str, *, tenant_id: str
) -> DomainEvent | None:
    event = (
        await db.execute(
            select(DomainEvent).where(
                DomainEvent.id == event_id,
                DomainEvent.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if not event or event.status not in ("failed", "dead"):
        return None
    event.status = "pending"
    event.max_attempts = max(event.max_attempts, event.attempts + 1)
    event.available_at = utcnow()
    event.last_error = ""
    for delivery in (
        await db.execute(
            select(EventDelivery).where(
                EventDelivery.event_id == event_id,
                EventDelivery.status.in_(("failed", "dead")),
            )
        )
    ).scalars().all():
        delivery.status = "pending"
        delivery.max_attempts = max(delivery.max_attempts, delivery.attempts + 1)
        delivery.next_attempt_at = utcnow()
        delivery.finished_at = None
        delivery.last_error = ""
    await db.commit()
    await db.refresh(event)
    return event


async def get_event(
    db: AsyncSession, event_id: str, *, tenant_id: str
) -> DomainEvent | None:
    return (
        await db.execute(
            select(DomainEvent).where(
                DomainEvent.id == event_id,
                DomainEvent.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()


async def get_event_deliveries(
    db: AsyncSession, event_id: str, *, tenant_id: str
) -> list[EventDelivery]:
    return list(
        (
            await db.execute(
                select(EventDelivery)
                .where(
                    EventDelivery.event_id == event_id,
                    EventDelivery.tenant_id == tenant_id,
                )
                .order_by(EventDelivery.subscriber.asc())
            )
        ).scalars().all()
    )


async def list_events(
    db: AsyncSession,
    *,
    tenant_id: str,
    status: str = "",
    event_type: str = "",
    aggregate_type: str = "",
    aggregate_id: str = "",
    page: int = 1,
    size: int = 20,
) -> tuple[int, list[DomainEvent]]:
    filters = [DomainEvent.tenant_id == tenant_id]
    if status:
        filters.append(DomainEvent.status == status)
    if event_type:
        filters.append(DomainEvent.event_type == event_type)
    if aggregate_type:
        filters.append(DomainEvent.aggregate_type == aggregate_type)
    if aggregate_id:
        filters.append(DomainEvent.aggregate_id == aggregate_id)
    total = (
        await db.execute(select(func.count(DomainEvent.id)).where(*filters))
    ).scalar_one()
    rows = (
        await db.execute(
            select(DomainEvent)
            .where(*filters)
            .order_by(DomainEvent.occurred_at.desc())
            .offset((max(1, page) - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return int(total), list(rows)


async def event_stats(db: AsyncSession, *, tenant_id: str) -> dict[str, int]:
    rows = (
        await db.execute(
            select(DomainEvent.status, func.count(DomainEvent.id))
            .where(DomainEvent.tenant_id == tenant_id)
            .group_by(DomainEvent.status)
        )
    ).all()
    data = {
        state: 0
        for state in ("pending", "dispatching", "published", "failed", "dead")
    }
    data.update({str(status): int(count) for status, count in rows})
    data["total"] = sum(data.values())
    return data


def event_to_dict(event: DomainEvent, deliveries: list[EventDelivery] | None = None) -> dict:
    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    data = {
        "id": event.id,
        "tenantId": event.tenant_id,
        "eventType": event.event_type,
        "source": event.source,
        "aggregateType": event.aggregate_type,
        "aggregateId": event.aggregate_id,
        "payload": event.payload or {},
        "headers": event.headers or {},
        "schemaVersion": event.schema_version,
        "status": event.status,
        "attempts": event.attempts,
        "maxAttempts": event.max_attempts,
        "idempotencyKey": event.idempotency_key or "",
        "correlationId": event.correlation_id,
        "causationId": event.causation_id,
        "lastError": event.last_error,
        "occurredAt": iso(event.occurred_at),
        "availableAt": iso(event.available_at),
        "publishedAt": iso(event.published_at),
    }
    if deliveries is not None:
        data["deliveries"] = [
            {
                "id": row.id,
                "subscriber": row.subscriber,
                "pattern": row.pattern,
                "status": row.status,
                "attempts": row.attempts,
                "maxAttempts": row.max_attempts,
                "lastError": row.last_error,
                "finishedAt": iso(row.finished_at),
            }
            for row in deliveries
        ]
    return data
