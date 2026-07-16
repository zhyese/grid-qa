"""事件订阅注册表与稳定的事件发布入口。"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from app.db.session import AsyncSessionLocal


@dataclass(frozen=True)
class EventContext:
    event_id: str
    event_type: str
    tenant_id: str
    source: str
    aggregate_type: str
    aggregate_id: str
    correlation_id: str
    causation_id: str
    occurred_at: datetime
    delivery_attempt: int


EventHandler = Callable[[dict, EventContext], Awaitable[Any] | Any]


@dataclass(frozen=True)
class EventSubscription:
    pattern: str
    subscriber: str
    handler: EventHandler
    max_attempts: int = 5


_SUBSCRIPTIONS: dict[str, EventSubscription] = {}


def register_event_handler(
    pattern: str,
    subscriber: str,
    handler: EventHandler,
    *,
    max_attempts: int = 5,
) -> EventHandler:
    """注册进程内订阅者；subscriber 名称必须稳定且全局唯一。"""
    pattern = pattern.strip()
    subscriber = subscriber.strip()
    if not pattern or not subscriber:
        raise ValueError("事件 pattern 和 subscriber 不能为空")
    current = _SUBSCRIPTIONS.get(subscriber)
    if current and current.handler is not handler:
        raise ValueError(f"事件订阅者重复注册: {subscriber}")
    _SUBSCRIPTIONS[subscriber] = EventSubscription(
        pattern=pattern,
        subscriber=subscriber,
        handler=handler,
        max_attempts=max(1, min(100, int(max_attempts))),
    )
    return handler


def subscribe_event(
    pattern: str, *, subscriber: str = "", max_attempts: int = 5
):
    """订阅装饰器，pattern 支持 ``*``，例如 ``scada.alert.*``。"""

    def decorator(handler: EventHandler) -> EventHandler:
        stable_name = subscriber or f"{handler.__module__}.{handler.__qualname__}"
        return register_event_handler(
            pattern, stable_name, handler, max_attempts=max_attempts
        )

    return decorator


def matching_subscriptions(event_type: str) -> list[EventSubscription]:
    return sorted(
        (
            sub
            for sub in _SUBSCRIPTIONS.values()
            if fnmatch.fnmatchcase(event_type, sub.pattern)
        ),
        key=lambda item: item.subscriber,
    )


def registered_subscriptions() -> list[dict]:
    return [
        {
            "subscriber": item.subscriber,
            "pattern": item.pattern,
            "maxAttempts": item.max_attempts,
        }
        for item in sorted(_SUBSCRIPTIONS.values(), key=lambda x: x.subscriber)
    ]


async def publish_event(
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
    max_attempts: int = 5,
) -> str:
    """稳定发布入口，返回新建或幂等命中的事件 ID。"""
    from app.services.event_center_service import publish_event_record

    async with AsyncSessionLocal() as db:
        event = await publish_event_record(
            db,
            event_type,
            payload,
            source=source,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            headers=headers,
            correlation_id=correlation_id,
            causation_id=causation_id,
            max_attempts=max_attempts,
        )
        return event.id

