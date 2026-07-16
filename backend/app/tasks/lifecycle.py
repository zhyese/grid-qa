"""FastAPI lifespan 可直接调用的 task/event worker 启停函数。"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable

from app.events.worker import event_dispatcher_loop
from app.tasks.worker import task_worker_loop


async def start_background_workers(
    app,
    *,
    queues: Iterable[str] = ("realtime", "default", "low"),
    task_poll_interval: float = 0.5,
    event_poll_interval: float = 0.5,
    stale_after_seconds: int = 300,
) -> list[asyncio.Task]:
    """启动各优先队列 worker 与一个事件 dispatcher；重复调用不会重复启动。"""
    current = getattr(app.state, "task_event_workers", None)
    if current:
        return current
    stop_event = asyncio.Event()
    workers = [
        asyncio.create_task(
            task_worker_loop(
                queue=queue,
                stop_event=stop_event,
                poll_interval=task_poll_interval,
                stale_after_seconds=stale_after_seconds,
            ),
            name=f"persistent-task-{queue}",
        )
        for queue in dict.fromkeys(q.strip() for q in queues if q.strip())
    ]
    workers.append(
        asyncio.create_task(
            event_dispatcher_loop(
                stop_event=stop_event,
                poll_interval=event_poll_interval,
                stale_after_seconds=stale_after_seconds,
            ),
            name="domain-event-dispatcher",
        )
    )
    app.state.task_event_stop = stop_event
    app.state.task_event_workers = workers
    return workers


async def stop_background_workers(app) -> None:
    stop_event = getattr(app.state, "task_event_stop", None)
    workers = getattr(app.state, "task_event_workers", [])
    if stop_event:
        stop_event.set()
    if workers:
        try:
            await asyncio.wait_for(
                asyncio.gather(*workers, return_exceptions=True), timeout=5.0
            )
        except TimeoutError:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
    app.state.task_event_workers = []
    app.state.task_event_stop = None


# 短别名，便于 lifespan 集成。
start_workers = start_background_workers
stop_workers = stop_background_workers
