"""任务 handler 注册表与稳定入队入口。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.db.session import AsyncSessionLocal


@dataclass(frozen=True)
class TaskContext:
    task_id: str
    task_type: str
    tenant_id: str
    queue: str
    attempt: int
    max_attempts: int
    correlation_id: str
    causation_id: str
    worker_id: str


TaskHandler = Callable[..., Awaitable[Any] | Any]
_HANDLERS: dict[str, TaskHandler] = {}


def register_task_handler(task_type: str, handler: TaskHandler) -> TaskHandler:
    task_type = task_type.strip()
    if not task_type:
        raise ValueError("task_type 不能为空")
    current = _HANDLERS.get(task_type)
    if current and current is not handler:
        raise ValueError(f"任务 handler 重复注册: {task_type}")
    _HANDLERS[task_type] = handler
    return handler


def task_handler(task_type: str):
    """注册任务 handler 的装饰器。handler 可接收 ``(payload, context)``。"""

    def decorator(handler: TaskHandler) -> TaskHandler:
        return register_task_handler(task_type, handler)

    return decorator


def get_task_handler(task_type: str) -> TaskHandler | None:
    return _HANDLERS.get(task_type)


def registered_task_types() -> list[str]:
    return sorted(_HANDLERS)


async def enqueue_task(
    task_type: str,
    payload: dict | None = None,
    *,
    queue: str = "default",
    idempotency_key: str = "",
    tenant_id: str = "default",
    priority: int = 0,
    max_attempts: int = 3,
    correlation_id: str = "",
    causation_id: str = "",
) -> str:
    """稳定入队入口，返回新建或幂等命中的任务 ID。"""
    from app.services.task_queue_service import enqueue_task_record

    async with AsyncSessionLocal() as db:
        task = await enqueue_task_record(
            db,
            task_type,
            payload,
            queue=queue,
            idempotency_key=idempotency_key,
            tenant_id=tenant_id,
            priority=priority,
            max_attempts=max_attempts,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        return task.id


async def enqueue(
    queue: str,
    task_name: str,
    payload: dict | None = None,
    **kwargs,
) -> str:
    """兼容项目既有 ``enqueue(queue, task_name)`` 调用。"""
    return await enqueue_task(task_name, payload, queue=queue, **kwargs)

