"""持久化任务队列与领域事件中心管理 API。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import AUDIT_READ, SYSTEM_CONFIG
from app.core.response import BizError, success
from app.db.session import get_db
from app.dependencies import require_perm
from app.events.registry import registered_subscriptions
from app.models.user import User
from app.schemas.task_center import (
    EnqueueTaskRequest,
    PublishEventRequest,
    TerminateTaskRequest,
)
from app.services import event_center_service, task_queue_service
from app.tasks.registry import registered_task_types

# 注册轻量内建 handler，使管理 API 能准确展示当前进程能力。
from app.tasks import builtin as _builtin  # noqa: F401, E402


router = APIRouter(prefix="/system/task-center", tags=["任务队列与事件中心"])


@router.get("/registry")
async def registry(
    user: User = Depends(require_perm(AUDIT_READ)),
):
    return success(
        {
            "taskTypes": registered_task_types(),
            "eventSubscriptions": registered_subscriptions(),
        },
        "查询成功",
    )


@router.post("/tasks")
async def enqueue_task_api(
    body: EnqueueTaskRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(SYSTEM_CONFIG)),
):
    task = await task_queue_service.enqueue_task_record(
        db,
        body.taskType,
        body.payload,
        queue=body.queue,
        idempotency_key=body.idempotencyKey,
        tenant_id=user.tenant_id,
        priority=body.priority,
        max_attempts=body.maxAttempts,
        run_after=body.runAfter,
        correlation_id=body.correlationId,
        causation_id=body.causationId,
    )
    return success(task_queue_service.task_to_dict(task), "任务已入队")


@router.get("/tasks")
async def list_tasks_api(
    status: str = Query("", max_length=16),
    taskType: str = Query("", max_length=128),
    queue: str = Query("", max_length=64),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(AUDIT_READ)),
):
    total, rows = await task_queue_service.list_tasks(
        db,
        tenant_id=user.tenant_id,
        status=status,
        task_type=taskType,
        queue=queue,
        page=page,
        size=size,
    )
    return success(
        {
            "total": total,
            "page": page,
            "size": size,
            "list": [task_queue_service.task_to_dict(row) for row in rows],
        },
        "查询成功",
    )


@router.get("/tasks/{task_id}")
async def task_detail_api(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(AUDIT_READ)),
):
    task = await task_queue_service.get_task(db, task_id, tenant_id=user.tenant_id)
    if not task:
        raise BizError("任务不存在", 404)
    return success(task_queue_service.task_to_dict(task), "查询成功")


@router.post("/tasks/{task_id}/retry")
async def retry_task_api(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(SYSTEM_CONFIG)),
):
    task = await task_queue_service.retry_task(db, task_id, tenant_id=user.tenant_id)
    if not task:
        raise BizError("任务不存在或当前状态不可重试", 409)
    return success(task_queue_service.task_to_dict(task), "任务已重新入队")


@router.post("/tasks/{task_id}/terminate")
async def terminate_task_api(
    task_id: str,
    body: TerminateTaskRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(SYSTEM_CONFIG)),
):
    task = await task_queue_service.move_task_to_dead(
        db, task_id, tenant_id=user.tenant_id, reason=body.reason
    )
    if not task:
        raise BizError("任务不存在、正在运行或已处于终态", 409)
    return success(task_queue_service.task_to_dict(task), "任务已终止")


@router.post("/events")
async def publish_event_api(
    body: PublishEventRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(SYSTEM_CONFIG)),
):
    event = await event_center_service.publish_event_record(
        db,
        body.eventType,
        body.payload,
        source=body.source,
        aggregate_type=body.aggregateType,
        aggregate_id=body.aggregateId,
        tenant_id=user.tenant_id,
        idempotency_key=body.idempotencyKey,
        headers=body.headers,
        correlation_id=body.correlationId,
        causation_id=body.causationId,
        schema_version=body.schemaVersion,
        max_attempts=body.maxAttempts,
    )
    return success(event_center_service.event_to_dict(event), "事件已写入 Outbox")


@router.get("/events")
async def list_events_api(
    status: str = Query("", max_length=16),
    eventType: str = Query("", max_length=160),
    aggregateType: str = Query("", max_length=128),
    aggregateId: str = Query("", max_length=128),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(AUDIT_READ)),
):
    total, rows = await event_center_service.list_events(
        db,
        tenant_id=user.tenant_id,
        status=status,
        event_type=eventType,
        aggregate_type=aggregateType,
        aggregate_id=aggregateId,
        page=page,
        size=size,
    )
    return success(
        {
            "total": total,
            "page": page,
            "size": size,
            "list": [event_center_service.event_to_dict(row) for row in rows],
        },
        "查询成功",
    )


@router.get("/events/{event_id}")
async def event_detail_api(
    event_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(AUDIT_READ)),
):
    event = await event_center_service.get_event(
        db, event_id, tenant_id=user.tenant_id
    )
    if not event:
        raise BizError("事件不存在", 404)
    deliveries = await event_center_service.get_event_deliveries(
        db, event_id, tenant_id=user.tenant_id
    )
    return success(
        event_center_service.event_to_dict(event, deliveries), "查询成功"
    )


@router.post("/events/{event_id}/retry")
async def retry_event_api(
    event_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(SYSTEM_CONFIG)),
):
    event = await event_center_service.retry_event(
        db, event_id, tenant_id=user.tenant_id
    )
    if not event:
        raise BizError("事件不存在或当前状态不可重试", 409)
    return success(event_center_service.event_to_dict(event), "事件已重新入队")


@router.get("/stats")
async def task_event_stats_api(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(AUDIT_READ)),
):
    tasks = await task_queue_service.task_stats(db, tenant_id=user.tenant_id)
    events = await event_center_service.event_stats(db, tenant_id=user.tenant_id)
    return success({"tasks": tasks, "events": events}, "查询成功")
