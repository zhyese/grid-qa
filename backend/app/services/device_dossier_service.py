"""按规范设备标识连接事件、主动运维记录及票据，不做模糊名称合并。"""

from sqlalchemy import select

from app.core.response import BizError
from app.models.realtime_event import (
    ProactiveOpsRun,
    RealtimeDeviceMapping,
    RealtimeEvent,
)
from app.models.ticket import Ticket
from app.services.ops_snapshot_service import create_snapshot, stamp


async def dossier(db, user, device_id):
    mappings = (
        (
            await db.execute(
                select(RealtimeDeviceMapping).where(
                    RealtimeDeviceMapping.tenant_id == user.tenant_id,
                    RealtimeDeviceMapping.canonical_device_id == device_id,
                    RealtimeDeviceMapping.active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    if not mappings:
        raise BizError("设备未建立有效规范ID映射，请先在主动运维中配置", 404)
    rows = (
        await db.execute(
            select(RealtimeEvent, ProactiveOpsRun, Ticket)
            .outerjoin(
                ProactiveOpsRun,
                (ProactiveOpsRun.event_ref_id == RealtimeEvent.id)
                & (ProactiveOpsRun.tenant_id == user.tenant_id),
            )
            .outerjoin(
                Ticket,
                (Ticket.id == ProactiveOpsRun.ticket_id)
                & (Ticket.tenant_id == user.tenant_id)
                & (Ticket.is_deleted == 0),
            )
            .where(
                RealtimeEvent.tenant_id == user.tenant_id,
                RealtimeEvent.canonical_device_id == device_id,
            )
            .order_by(RealtimeEvent.occurred_at.desc(), RealtimeEvent.id)
            .limit(501)
        )
    ).all()
    if len(rows) > 500:
        raise BizError("设备档案超过500条关联记录，需增加分页后再导出完整复盘", 400)
    timeline = []
    for event, run, ticket in rows:
        timeline.append(
            {
                "at": stamp(event.occurred_at),
                "eventId": event.id,
                "title": event.title,
                "severity": event.severity,
                "source": event.source,
                "runId": run.id if run else None,
                "runStatus": run.status if run else None,
                "ticketId": ticket.id if ticket else None,
                "ticketStatus": ticket.status.value if ticket else None,
            }
        )
    return {
        "deviceId": device_id,
        "names": sorted({m.canonical_name for m in mappings}),
        "stations": sorted({m.station for m in mappings}),
        "timeline": timeline,
        "scope": "仅规范ID事件及显式关联票据；未关联人工票据不自动归属。状态为当前状态，非历史还原。",
    }


async def snapshot(db, user, device_id):
    data = await dossier(db, user, device_id)
    return await create_snapshot(
        db, user, "dossier", f"设备复盘 {device_id}"[:200], data
    )
