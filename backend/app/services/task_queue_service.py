"""DB-backed 持久化任务队列的事务服务。"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.persistent_task import PersistentTask


CLAIMABLE_STATUSES = ("queued", "failed")
FINAL_STATUSES = ("succeeded", "dead")


def utcnow() -> datetime:
    """返回适配 MySQL ``DATETIME`` 的 naive UTC 时间。"""
    return datetime.now(UTC).replace(tzinfo=None)


def json_safe(value: Any) -> Any:
    """将 handler 返回值规整为可写入 JSON 列的对象。"""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


async def enqueue_task_record(
    db: AsyncSession,
    task_type: str,
    payload: dict | None = None,
    *,
    queue: str = "default",
    idempotency_key: str = "",
    tenant_id: str = "default",
    priority: int = 0,
    max_attempts: int = 3,
    run_after: datetime | None = None,
    correlation_id: str = "",
    causation_id: str = "",
    commit: bool = True,
) -> PersistentTask:
    """创建任务；同租户、同任务类型和同幂等键只创建一次。

    ``commit=False`` 可把任务与业务变更放入同一事务，调用方负责最终提交。
    """
    task_type = task_type.strip()
    if not task_type:
        raise ValueError("task_type 不能为空")
    tenant_id = tenant_id.strip() or "default"
    queue = queue.strip() or "default"
    idem = idempotency_key.strip() or None

    if idem:
        existing = (
            await db.execute(
                select(PersistentTask).where(
                    PersistentTask.tenant_id == tenant_id,
                    PersistentTask.task_type == task_type,
                    PersistentTask.idempotency_key == idem,
                )
            )
        ).scalar_one_or_none()
        if existing:
            return existing

    task = PersistentTask(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        queue_name=queue,
        task_type=task_type,
        payload=json_safe(payload or {}),
        status="queued",
        priority=max(-100, min(100, int(priority))),
        attempts=0,
        max_attempts=max(1, min(100, int(max_attempts))),
        run_after=run_after or utcnow(),
        idempotency_key=idem,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )
    try:
        # 幂等竞争只回滚保存点，不能把调用方同一事务中的业务变更一起回滚。
        async with db.begin_nested():
            db.add(task)
            await db.flush()
        if commit:
            await db.commit()
            await db.refresh(task)
        return task
    except IntegrityError:
        # 并发请求可能同时通过前置查询，唯一约束是最终防线。
        if not idem:
            raise
        existing = (
            await db.execute(
                select(PersistentTask).where(
                    PersistentTask.tenant_id == tenant_id,
                    PersistentTask.task_type == task_type,
                    PersistentTask.idempotency_key == idem,
                )
            )
        ).scalar_one()
        return existing


async def claim_next_task(
    db: AsyncSession,
    *,
    queue: str,
    worker_id: str,
) -> PersistentTask | None:
    """用 ``FOR UPDATE SKIP LOCKED`` 原子领取一个到期任务。"""
    now = utcnow()
    stmt = (
        select(PersistentTask)
        .where(
            PersistentTask.queue_name == queue,
            PersistentTask.status.in_(CLAIMABLE_STATUSES),
            PersistentTask.run_after <= now,
            PersistentTask.attempts < PersistentTask.max_attempts,
        )
        .order_by(PersistentTask.priority.desc(), PersistentTask.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    task = (await db.execute(stmt)).scalar_one_or_none()
    if not task:
        await db.rollback()
        return None
    task.status = "running"
    task.attempts += 1
    task.locked_by = worker_id
    task.locked_at = now
    task.heartbeat_at = now
    task.last_error = ""
    await db.commit()
    await db.refresh(task)
    return task


async def heartbeat_task(
    db: AsyncSession, task_id: str, *, worker_id: str
) -> bool:
    task = (
        await db.execute(
            select(PersistentTask)
            .where(PersistentTask.id == task_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not task or task.status != "running" or task.locked_by != worker_id:
        return False
    task.heartbeat_at = utcnow()
    await db.commit()
    return True


async def complete_task(
    db: AsyncSession, task_id: str, *, worker_id: str, result: Any = None
) -> bool:
    task = (
        await db.execute(
            select(PersistentTask)
            .where(PersistentTask.id == task_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not task or task.status != "running" or task.locked_by != worker_id:
        return False
    task.status = "succeeded"
    task.result = json_safe(result)
    task.finished_at = utcnow()
    task.locked_by = ""
    task.locked_at = None
    task.heartbeat_at = None
    await db.commit()
    return True


async def fail_task(
    db: AsyncSession,
    task_id: str,
    *,
    worker_id: str,
    error: str,
    retry_delay_seconds: float | None = None,
) -> str | None:
    """记录失败；仍有次数时进入 ``failed`` 等待重试，否则进入 ``dead``。"""
    task = (
        await db.execute(
            select(PersistentTask)
            .where(PersistentTask.id == task_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not task or task.status != "running" or task.locked_by != worker_id:
        return None
    task.last_error = error[-8000:]
    task.locked_by = ""
    task.locked_at = None
    task.heartbeat_at = None
    if task.attempts >= task.max_attempts:
        task.status = "dead"
        task.finished_at = utcnow()
    else:
        task.status = "failed"
        delay = (
            retry_delay_seconds
            if retry_delay_seconds is not None
            else min(300.0, float(2 ** max(0, task.attempts - 1)))
        )
        task.run_after = utcnow() + timedelta(seconds=max(0.0, delay))
    await db.commit()
    return task.status


async def retry_task(
    db: AsyncSession, task_id: str, *, tenant_id: str
) -> PersistentTask | None:
    task = (
        await db.execute(
            select(PersistentTask).where(
                PersistentTask.id == task_id,
                PersistentTask.tenant_id == tenant_id,
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if not task or task.status not in ("failed", "dead"):
        return None
    task.status = "queued"
    task.max_attempts = max(task.max_attempts, task.attempts + 1)
    task.run_after = utcnow()
    task.finished_at = None
    task.last_error = ""
    task.locked_by = ""
    task.locked_at = None
    await db.commit()
    await db.refresh(task)
    return task


async def move_task_to_dead(
    db: AsyncSession, task_id: str, *, tenant_id: str, reason: str
) -> PersistentTask | None:
    """人工终止尚未完成的任务；使用既有 ``dead`` 终态，不引入隐藏状态。"""
    task = (
        await db.execute(
            select(PersistentTask).where(
                PersistentTask.id == task_id,
                PersistentTask.tenant_id == tenant_id,
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if not task or task.status in FINAL_STATUSES or task.status == "running":
        return None
    task.status = "dead"
    task.last_error = (reason or "人工终止")[-8000:]
    task.finished_at = utcnow()
    task.locked_by = ""
    task.locked_at = None
    task.heartbeat_at = None
    await db.commit()
    await db.refresh(task)
    return task


async def recover_stale_tasks(
    db: AsyncSession, *, stale_after_seconds: int = 300
) -> int:
    """回收因 worker 崩溃长期停留在 running 的任务。"""
    cutoff = utcnow() - timedelta(seconds=max(1, stale_after_seconds))
    rows = (
        await db.execute(
            select(PersistentTask)
            .where(
                PersistentTask.status == "running",
                or_(
                    PersistentTask.heartbeat_at < cutoff,
                    PersistentTask.heartbeat_at.is_(None),
                ),
            )
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    for task in rows:
        task.locked_by = ""
        task.locked_at = None
        task.heartbeat_at = None
        task.last_error = "worker 心跳超时，任务已回收"
        if task.attempts >= task.max_attempts:
            task.status = "dead"
            task.finished_at = utcnow()
        else:
            task.status = "failed"
            task.run_after = utcnow()
    await db.commit()
    return len(rows)


async def get_task(
    db: AsyncSession, task_id: str, *, tenant_id: str
) -> PersistentTask | None:
    return (
        await db.execute(
            select(PersistentTask).where(
                PersistentTask.id == task_id,
                PersistentTask.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()


async def list_tasks(
    db: AsyncSession,
    *,
    tenant_id: str,
    status: str = "",
    task_type: str = "",
    queue: str = "",
    page: int = 1,
    size: int = 20,
) -> tuple[int, list[PersistentTask]]:
    filters = [PersistentTask.tenant_id == tenant_id]
    if status:
        filters.append(PersistentTask.status == status)
    if task_type:
        filters.append(PersistentTask.task_type == task_type)
    if queue:
        filters.append(PersistentTask.queue_name == queue)
    total = (
        await db.execute(select(func.count(PersistentTask.id)).where(*filters))
    ).scalar_one()
    rows = (
        await db.execute(
            select(PersistentTask)
            .where(*filters)
            .order_by(PersistentTask.created_at.desc())
            .offset((max(1, page) - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return int(total), list(rows)


async def task_stats(db: AsyncSession, *, tenant_id: str) -> dict[str, int]:
    rows = (
        await db.execute(
            select(PersistentTask.status, func.count(PersistentTask.id))
            .where(PersistentTask.tenant_id == tenant_id)
            .group_by(PersistentTask.status)
        )
    ).all()
    data = {state: 0 for state in ("queued", "running", "succeeded", "failed", "dead")}
    data.update({str(status): int(count) for status, count in rows})
    data["total"] = sum(data.values())
    return data


def task_to_dict(task: PersistentTask) -> dict:
    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "id": task.id,
        "tenantId": task.tenant_id,
        "queue": task.queue_name,
        "taskType": task.task_type,
        "payload": task.payload or {},
        "status": task.status,
        "priority": task.priority,
        "attempts": task.attempts,
        "maxAttempts": task.max_attempts,
        "runAfter": iso(task.run_after),
        "idempotencyKey": task.idempotency_key or "",
        "correlationId": task.correlation_id,
        "causationId": task.causation_id,
        "lockedBy": task.locked_by,
        "lastError": task.last_error,
        "result": task.result,
        "createdAt": iso(task.created_at),
        "updatedAt": iso(task.updated_at),
        "finishedAt": iso(task.finished_at),
    }
