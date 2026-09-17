"""班次快照：事件按发生时间，票据为生成时的未结束状态。"""

from sqlalchemy import and_, or_, select

from app.core.response import BizError
from app.models.realtime_event import RealtimeEvent
from app.models.ticket import Ticket, TicketStatus
from app.services.ops_snapshot_service import create_snapshot, stamp


async def generate(db, user, body):
    start, end = body.start.replace(tzinfo=None), body.end.replace(tzinfo=None)
    events = (
        (
            await db.execute(
                select(RealtimeEvent)
                .where(
                    RealtimeEvent.tenant_id == user.tenant_id,
                    RealtimeEvent.occurred_at >= start,
                    RealtimeEvent.occurred_at < end,
                )
                .order_by(RealtimeEvent.occurred_at, RealtimeEvent.id)
                .limit(1001)
            )
        )
        .scalars()
        .all()
    )
    tickets = (
        (
            await db.execute(
                select(Ticket)
                .where(
                    Ticket.tenant_id == user.tenant_id,
                    Ticket.is_deleted == 0,
                    Ticket.created_at < end,
                    or_(
                        Ticket.status.notin_(
                            [
                                TicketStatus.COMPLETED,
                                TicketStatus.ARCHIVED,
                                TicketStatus.REJECTED,
                            ]
                        ),
                        and_(Ticket.updated_at >= start, Ticket.updated_at < end),
                    ),
                )
                .order_by(Ticket.created_at, Ticket.id)
                .limit(1001)
            )
        )
        .scalars()
        .all()
    )
    if len(events) > 1000 or len(tickets) > 1000:
        raise BizError("班次数据超过1000条，请缩短时间范围；不会静默省略事项", 400)
    payload = {
        "start": body.start.isoformat(),
        "end": body.end.isoformat(),
        "events": [
            {
                "id": e.id,
                "title": e.title,
                "severity": e.severity,
                "deviceId": e.canonical_device_id,
                "at": stamp(e.occurred_at),
                "status": e.processing_status,
            }
            for e in events
        ],
        "tickets": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "device": t.device,
                "updatedAt": stamp(t.updated_at),
            }
            for t in tickets
        ],
        "note": "事件区间为[start,end)；票据为生成时当前状态，包含未结束事项及区间起点后更新事项，不代表历史时点状态。",
    }
    return await create_snapshot(db, user, "handover", body.title, payload)
