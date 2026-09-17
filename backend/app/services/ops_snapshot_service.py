"""租户内证据快照及人工流转；无后台自动审批。"""

import json
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.core.response import BizError
from app.models.document import Document
from app.models.ops_workbench import OpsSnapshot


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def stamp(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


async def readable_document(db, user, doc_id):
    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise BizError("文档不存在或不可访问", 404)
    if user.role != "admin":
        roles = {v.strip() for v in (doc.allowed_roles or "").split(",") if v.strip()}
        if (doc.dept and doc.dept != (user.dept or "")) or (
            roles and user.role not in roles
        ):
            raise BizError("文档不存在或不可访问", 404)
    return doc


async def create_snapshot(db, user, kind, title, payload):
    row = OpsSnapshot(
        tenant_id=user.tenant_id,
        kind=kind,
        title=title,
        payload_json=dump(payload),
        creator=user.username,
        audit_json="[]",
        status="draft",
        version=1,
    )
    db.add(row)
    await db.flush()
    await db.commit()
    return serialize(row)


def serialize(row):
    return {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "status": row.status,
        "version": row.version,
        "creator": row.creator,
        "createdAt": stamp(row.created_at),
        "payload": json.loads(row.payload_json),
        "audit": json.loads(row.audit_json),
    }


async def get_snapshot(db, user, snapshot_id):
    row = (
        await db.execute(
            select(OpsSnapshot).where(
                OpsSnapshot.id == snapshot_id,
                OpsSnapshot.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise BizError("记录不存在", 404)
    for doc_id in json.loads(row.payload_json).get("documentIds", []):
        await readable_document(db, user, doc_id)
    return row


async def list_snapshots(db, user, kind, page=1):
    # 返回最小列表；文档快照逐条检查当前 ACL，不能因历史快照绕过撤权。
    rows = (
        (
            await db.execute(
                select(OpsSnapshot)
                .where(
                    OpsSnapshot.tenant_id == user.tenant_id,
                    OpsSnapshot.kind == kind,
                )
                .order_by(OpsSnapshot.created_at.desc(), OpsSnapshot.id)
                .offset((page - 1) * 20)
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    result = []
    for row in rows:
        try:
            for doc_id in json.loads(row.payload_json).get("documentIds", []):
                await readable_document(db, user, doc_id)
        except BizError:
            continue
        result.append(
            {k: v for k, v in serialize(row).items() if k not in {"payload", "audit"}}
        )
    return {"list": result, "page": page, "pageSize": 20}


async def review_snapshot(db, user, snapshot_id, body):
    row = await get_snapshot(db, user, snapshot_id)
    expected = "draft" if body.action == "review" else "reviewed"
    target = "reviewed" if body.action == "review" else "accepted"
    if row.kind not in {"handover", "impact"} or (
        body.action == "accept" and row.kind != "handover"
    ):
        raise BizError("该记录不支持此操作", 400)
    audit = json.loads(row.audit_json)
    if body.action == "accept" and (
        user.username == row.creator
        or any(a["actor"] == user.username and a["action"] == "review" for a in audit)
    ):
        raise BizError("接班确认必须由不同于交班人及审核人的用户执行", 403)
    if row.status != expected or row.version != body.version:
        raise BizError("记录已更新，请刷新后重试", 409)
    audit.append(
        {
            "actor": user.username,
            "action": body.action,
            "note": body.note,
            "at": datetime.now(timezone.utc).isoformat(),
            "from": expected,
            "to": target,
        }
    )
    result = await db.execute(
        update(OpsSnapshot)
        .where(
            OpsSnapshot.id == row.id,
            OpsSnapshot.tenant_id == user.tenant_id,
            OpsSnapshot.version == body.version,
            OpsSnapshot.status == expected,
        )
        .values(status=target, version=body.version + 1, audit_json=dump(audit))
    )
    if result.rowcount != 1:
        await db.rollback()
        raise BizError("记录已更新，请刷新后重试", 409)
    await db.commit()
    await db.refresh(row)
    return serialize(row)
