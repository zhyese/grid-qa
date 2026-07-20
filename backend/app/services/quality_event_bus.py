"""质量事件总线：跨闭环信号统一 emit + subscribe 派发（数据飞轮 Task A1）。

底层逻辑：一次坏信号 → 总线 → 多订阅者并行(清理旧+补全新+评估改进) → 知识库更新 → 下次更好。
飞轮转速 = feedback_fix_rate（dislike→补全→同 query 再 like 比例）。

复用 _bg_tasks 异步派发订阅者，独立 session，异常 degraded 不阻塞 emit。
QUALITY_BUS_ENABLE=False 时 emit 仅入库不派发（关=现状，零破坏）。
"""
import asyncio
import fnmatch

from app.config import settings
from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.quality_event import QualityEvent

_bg_tasks: set = set()
_subscribers: list[tuple[str, object]] = []  # (pattern, async handler)


def subscribe(pattern: str, handler) -> None:
    """注册订阅者。pattern 支持 fnmatch：'feedback.*' / '*.dislike' / 'governance.*'。"""
    _subscribers.append((pattern, handler))


def reset_subscribers() -> None:
    """测试用：清订阅者。"""
    _subscribers.clear()


def _matches(source: str, type: str, pattern: str) -> bool:
    return fnmatch.fnmatch(f"{source}.{type}", pattern) or fnmatch.fnmatch(source, pattern)


async def emit(source: str, type: str, payload: dict | None = None,
               tenant: str = "default") -> str:
    """发质量事件：入库 +（开关开）异步派发匹配订阅者。返回 event_id（空=入库失败）。"""
    payload = payload or {}
    try:
        async with AsyncSessionLocal() as db:
            row = QualityEvent(source=source, type=type, payload=payload, tenant=tenant)
            db.add(row)
            await db.commit()
            await db.refresh(row)
            event_id = row.id
    except Exception as e:
        degraded("quality_event_emit", e)
        return ""
    if getattr(settings, "QUALITY_BUS_ENABLE", False):
        for pattern, handler in list(_subscribers):
            if _matches(source, type, pattern):
                try:
                    t = asyncio.create_task(
                        _safe_dispatch(handler, event_id, source, type, payload, tenant))
                    _bg_tasks.add(t)
                    t.add_done_callback(_bg_tasks.discard)
                except Exception as e:
                    degraded("quality_event_dispatch", e)
    return event_id


async def _safe_dispatch(handler, event_id, source, type, payload, tenant) -> None:
    """订阅者执行，异常 degraded 不阻塞总线。"""
    try:
        await handler(event_id, source, type, payload, tenant)
    except Exception as e:
        degraded("quality_event_handler", e)
