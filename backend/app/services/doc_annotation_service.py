"""文档协作批注服务：锚点批注 CRUD + 回复 + 解决（DOC_COLLAB_ENABLE 门控）。

批注挂到文档的 chunk 锚点（chunk_idx + quote 双锚），不改文档本体；
租户隔离 + 文档级 ACL 校验复用 document_service。
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import BizError
from app.models.doc_collab import DocAnnotation
from app.services import document_service


def _row(a: DocAnnotation) -> dict:
    return {"id": a.id, "docId": a.doc_id, "chunkIdx": a.chunk_idx, "quote": a.quote,
            "content": a.content, "status": a.status, "replies": a.replies or [],
            "resolvedBy": a.resolved_by,
            "resolvedAt": a.resolved_at.isoformat() if a.resolved_at else None,
            "author": a.author,
            "createdAt": a.created_at.isoformat() if a.created_at else None,
            "updatedAt": a.updated_at.isoformat() if a.updated_at else None}


async def _check_doc(db: AsyncSession, doc_id: str, tenant_id: str,
                     user_dept: str, user_role: str) -> None:
    """文档存在性 + 租户 + ACL（复用 document_service 的判定）。"""
    doc = await document_service.get_document(db, doc_id)
    if doc.tenant_id and doc.tenant_id != tenant_id:
        raise BizError("文档不存在", 404)
    document_service._assert_acl(doc, user_dept, user_role)


async def list_annotations(db: AsyncSession, doc_id: str, tenant_id: str,
                           status: str, user_dept: str, user_role: str) -> list[dict]:
    await _check_doc(db, doc_id, tenant_id, user_dept, user_role)
    conds = [DocAnnotation.doc_id == doc_id, DocAnnotation.tenant == tenant_id]
    if status:
        conds.append(DocAnnotation.status == status)
    rows = (await db.execute(
        select(DocAnnotation).where(*conds)
        .order_by(DocAnnotation.status, DocAnnotation.created_at.desc())
        .limit(500))).scalars().all()
    return [_row(a) for a in rows]


async def create_annotation(db: AsyncSession, doc_id: str, chunk_idx: int, quote: str,
                            content: str, tenant_id: str, username: str,
                            user_dept: str, user_role: str) -> dict:
    await _check_doc(db, doc_id, tenant_id, user_dept, user_role)
    if not (content or "").strip():
        raise BizError("批注内容不能为空", 400)
    import uuid

    a = DocAnnotation(id=uuid.uuid4().hex, doc_id=doc_id,
                      chunk_idx=max(0, int(chunk_idx or 0)),
                      quote=(quote or "")[:500], content=content.strip(),
                      tenant=tenant_id, author=username)
    db.add(a)
    await db.commit()
    await db.refresh(a)  # MySQL 无 RETURNING，server_default 列需 refresh 才可读
    return _row(a)


async def _get_annotation(db: AsyncSession, ann_id: str, tenant_id: str) -> DocAnnotation:
    a = (await db.execute(select(DocAnnotation).where(
        DocAnnotation.id == ann_id, DocAnnotation.tenant == tenant_id))).scalar_one_or_none()
    if a is None:
        raise BizError("批注不存在", 404)
    return a


async def reply_annotation(db: AsyncSession, ann_id: str, content: str,
                           tenant_id: str, username: str) -> dict:
    from datetime import datetime

    a = await _get_annotation(db, ann_id, tenant_id)
    if not (content or "").strip():
        raise BizError("回复内容不能为空", 400)
    replies = list(a.replies or [])
    replies.append({"author": username, "content": content.strip()[:500],
                    "at": datetime.now().isoformat()})
    a.replies = replies
    await db.commit()
    await db.refresh(a)  # onupdate(updated_at) 后属性过期，async session 禁属性级懒 IO
    return _row(a)


async def resolve_annotation(db: AsyncSession, ann_id: str, resolved: bool,
                             tenant_id: str, username: str) -> dict:
    from datetime import datetime

    a = await _get_annotation(db, ann_id, tenant_id)
    a.status = "resolved" if resolved else "open"
    a.resolved_by = username if resolved else ""
    a.resolved_at = datetime.now() if resolved else None
    await db.commit()
    await db.refresh(a)  # onupdate(updated_at) 后属性过期，async session 禁属性级懒 IO
    return _row(a)


async def delete_annotation(db: AsyncSession, ann_id: str, tenant_id: str,
                            username: str, is_admin: bool) -> dict:
    """作者本人或 admin 可删（编辑讨论的清理权）。"""
    a = await _get_annotation(db, ann_id, tenant_id)
    if not is_admin and a.author != username:
        raise BizError("仅批注作者或管理员可删除", 403)
    await db.delete(a)
    await db.commit()
    return {"id": ann_id}


async def annotation_stats(db: AsyncSession, doc_id: str, tenant_id: str) -> dict:
    rows = (await db.execute(
        select(DocAnnotation.status, func.count(DocAnnotation.id))
        .where(DocAnnotation.doc_id == doc_id, DocAnnotation.tenant == tenant_id)
        .group_by(DocAnnotation.status))).all()
    by_status = {st: n for st, n in rows}
    return {"total": sum(by_status.values()), "open": by_status.get("open", 0),
            "resolved": by_status.get("resolved", 0)}


async def to_governance_issue(db: AsyncSession, ann_id: str, tenant_id: str,
                              username: str) -> dict:
    """批注转治理 issue（annotation_comment）：协作讨论沉淀为治理工单。

    幂等：fingerprint 含批注 updated_at，内容变更后可再转（新的指纹）；
    重复转同版本返回 existing。
    """
    import hashlib
    import json
    from datetime import datetime

    from app.models.knowledge_governance import KnowledgeGovernanceIssue

    a = await _get_annotation(db, ann_id, tenant_id)
    fingerprint = hashlib.sha256(
        f"annotation:{a.id}:{a.updated_at or ''}".encode()).hexdigest()
    exists = (await db.execute(select(KnowledgeGovernanceIssue.id).where(
        KnowledgeGovernanceIssue.fingerprint == fingerprint))).scalar_one_or_none()
    if exists:
        return {"issueId": exists, "existing": True}
    issue = KnowledgeGovernanceIssue(
        tenant_id=tenant_id,
        fingerprint=fingerprint,
        issue_type="annotation_comment",
        severity="info",
        status="open",
        doc_id=a.doc_id,
        title=f"批注待治理：{a.content[:60]}",
        summary=(f"批注（{a.author}，chunk#{a.chunk_idx}）：{a.content}\n"
                 f"锚点原文：{a.quote or '无'}\n"
                 f"回复 {len(a.replies or [])} 条。由 {username} 转治理工单。"),
        evidence_json=json.dumps({
            "annotationId": a.id, "author": a.author, "chunkIdx": a.chunk_idx,
            "quote": a.quote, "content": a.content,
            "replies": (a.replies or [])[:5], "convertedBy": username,
        }, ensure_ascii=False),
        occurrence_count=1,
        detected_at=datetime.now(),
        last_seen_at=datetime.now(),
    )
    db.add(issue)
    await db.commit()
    await db.refresh(issue)  # server_default 列需 refresh 才可读
    return {"issueId": issue.id, "existing": False}
