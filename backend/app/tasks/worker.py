"""持久化任务 worker。"""
from __future__ import annotations

import asyncio
import inspect
import socket
import traceback
import uuid
from collections.abc import Callable

from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.services import task_queue_service
from app.tasks.registry import TaskContext, get_task_handler


def make_worker_id(queue: str) -> str:
    return f"{socket.gethostname()}:{queue}:{uuid.uuid4().hex[:8]}"


async def _invoke(handler, payload: dict, context: TaskContext):
    # 单参数 handler 便于简单任务；标准签名是 (payload, context)。
    try:
        parameter_count = len(inspect.signature(handler).parameters)
    except (TypeError, ValueError):
        parameter_count = 2
    result = handler(payload) if parameter_count == 1 else handler(payload, context)
    if inspect.isawaitable(result):
        return await result
    return result


async def _heartbeat_loop(
    task_id: str,
    worker_id: str,
    stop_event: asyncio.Event,
    session_factory: Callable,
    interval: float = 30.0,
) -> None:
    """长任务续租，防止其他 worker 把仍在运行的 Agent/扫描误判为僵尸任务。"""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(1.0, interval))
            break
        except TimeoutError:
            pass
        try:
            async with session_factory() as db:
                alive = await task_queue_service.heartbeat_task(
                    db, task_id, worker_id=worker_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 瞬时 DB 故障不应让长任务永久失去续租能力。
            degraded(
                "task_worker_heartbeat",
                exc,
                f"task={task_id} worker={worker_id}",
            )
            continue
        if not alive:
            break


async def run_task_once(
    *,
    queue: str = "default",
    worker_id: str | None = None,
    session_factory: Callable = AsyncSessionLocal,
) -> bool:
    """领取并执行一个任务；有任务（无论成功失败）返回 True。"""
    worker_id = worker_id or make_worker_id(queue)
    # 内建 handler 的导入本身具备幂等性。
    from app.tasks import builtin as _builtin  # noqa: F401

    async with session_factory() as db:
        task = await task_queue_service.claim_next_task(
            db, queue=queue, worker_id=worker_id
        )
    if not task:
        return False

    context = TaskContext(
        task_id=task.id,
        task_type=task.task_type,
        tenant_id=task.tenant_id,
        queue=task.queue_name,
        attempt=task.attempts,
        max_attempts=task.max_attempts,
        correlation_id=task.correlation_id,
        causation_id=task.causation_id,
        worker_id=worker_id,
    )
    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat_loop(task.id, worker_id, heartbeat_stop, session_factory),
        name=f"task-heartbeat-{task.id}",
    )
    try:
        handler = get_task_handler(task.task_type)
        if not handler:
            raise LookupError(f"未注册任务 handler: {task.task_type}")
        result = await _invoke(handler, task.payload or {}, context)
        async with session_factory() as db:
            await task_queue_service.complete_task(
                db, task.id, worker_id=worker_id, result=result
            )
    except asyncio.CancelledError:
        # shutdown 时保留 running，由下次启动的 stale 回收处理，防止误记业务失败。
        raise
    except Exception as exc:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        async with session_factory() as db:
            await task_queue_service.fail_task(
                db, task.id, worker_id=worker_id, error=detail
            )
    finally:
        heartbeat_stop.set()
        await asyncio.gather(heartbeat, return_exceptions=True)
    return True


async def task_worker_loop(
    *,
    queue: str,
    stop_event: asyncio.Event,
    poll_interval: float = 1.0,
    stale_after_seconds: int = 300,
    session_factory: Callable = AsyncSessionLocal,
) -> None:
    worker_id = make_worker_id(queue)
    last_recovery = 0.0
    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        worked = False
        try:
            now = loop.time()
            if now - last_recovery >= max(30.0, stale_after_seconds / 2):
                async with session_factory() as db:
                    await task_queue_service.recover_stale_tasks(
                        db, stale_after_seconds=stale_after_seconds
                    )
                last_recovery = now
            worked = await run_task_once(
                queue=queue, worker_id=worker_id, session_factory=session_factory
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 单次连接抖动/事务失败只降级本轮，worker 必须继续存活。
            degraded(
                "task_worker_loop",
                exc,
                f"queue={queue} worker={worker_id}",
            )
        if worked:
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, poll_interval))
        except TimeoutError:
            pass
