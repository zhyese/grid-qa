"""工作台选择器：租户及文档 ACL 前置，避免依赖手工查找内部 ID。"""

from sqlalchemy import select

from app.core.response import BizError
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.realtime_event import RealtimeDeviceMapping
from app.services.ops_snapshot_service import readable_document
from app.services.table_lookup_service import parse_table


async def documents(db, user, keyword):
    stmt = select(Document).where(Document.tenant_id == user.tenant_id)
    if keyword:
        stmt = stmt.where(Document.doc_name.contains(keyword, autoescape=True))
    rows = (
        (
            await db.execute(
                stmt.order_by(Document.created_at.desc(), Document.id).limit(201)
            )
        )
        .scalars()
        .all()
    )
    result = []
    for row in rows[:200]:
        try:
            await readable_document(db, user, row.id)
        except BizError:
            continue
        result.append({"id": row.id, "name": row.doc_name, "status": row.status})
    return {"list": result, "hasMore": len(rows) > 200}


async def devices(db, user, keyword):
    stmt = select(RealtimeDeviceMapping).where(
        RealtimeDeviceMapping.tenant_id == user.tenant_id,
        RealtimeDeviceMapping.active.is_(True),
    )
    if keyword:
        stmt = stmt.where(
            RealtimeDeviceMapping.canonical_name.contains(keyword, autoescape=True)
            | RealtimeDeviceMapping.canonical_device_id.contains(
                keyword, autoescape=True
            )
        )
    rows = (
        (
            await db.execute(
                stmt.order_by(RealtimeDeviceMapping.canonical_device_id).limit(201)
            )
        )
        .scalars()
        .all()
    )
    return {
        "list": [
            {
                "id": r.canonical_device_id,
                "name": r.canonical_name,
                "station": r.station,
            }
            for r in rows[:200]
        ],
        "hasMore": len(rows) > 200,
    }


async def table_schema(db, user, doc_id):
    await readable_document(db, user, doc_id)
    rows = (
        (
            await db.execute(
                select(Chunk)
                .where(Chunk.doc_id == doc_id, Chunk.chunk_type == "table")
                .order_by(Chunk.chunk_idx)
                .limit(501)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) > 500:
        raise BizError("表格超过500块，请按章节拆分文档", 400)
    tables = []
    for row in rows:
        parsed = parse_table(row.content)
        tables.append(
            {
                "chunkId": row.id,
                "page": row.page_num,
                "columns": parsed[0] if parsed else [],
                "parseable": parsed is not None,
                "preview": parsed[1][:3] if parsed else [],
            }
        )
    return {"tables": tables}
