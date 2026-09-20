"""文档协作批注 + 电子签批 API（/api/doc-collab，DOC_COLLAB_ENABLE 开时注册）。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import DOC_ANNOTATE, DOC_READ, DOC_SIGNOFF
from app.core.response import success
from app.db.session import get_db
from app.dependencies import require_perm
from app.models.user import User
from app.schemas.doc_collab import (
    AnnotationCreate,
    AnnotationReply,
    AnnotationResolve,
    SignoffAction,
    SignoffCreate,
)
from app.services import doc_annotation_service as ann
from app.services import doc_signoff_service as signoff

router = APIRouter(prefix="/doc-collab", tags=["文档协作"])


# ---------- 批注 ----------

@router.get("/documents/{doc_id}/annotations")
async def list_annotations_api(
    doc_id: str,
    status: str = Query(""),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_READ)),
):
    rows = await ann.list_annotations(db, doc_id, user.tenant_id, status,
                                      user.dept, user.role)
    return success(rows)


@router.post("/documents/{doc_id}/annotations")
async def create_annotation_api(
    doc_id: str,
    body: AnnotationCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_ANNOTATE)),
):
    return success(await ann.create_annotation(
        db, doc_id, body.chunkIdx, body.quote, body.content,
        user.tenant_id, user.username, user.dept, user.role))


@router.post("/annotations/{ann_id}/reply")
async def reply_annotation_api(
    ann_id: str,
    body: AnnotationReply,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_ANNOTATE)),
):
    return success(await ann.reply_annotation(db, ann_id, body.content,
                                              user.tenant_id, user.username))


@router.post("/annotations/{ann_id}/resolve")
async def resolve_annotation_api(
    ann_id: str,
    body: AnnotationResolve,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_ANNOTATE)),
):
    return success(await ann.resolve_annotation(db, ann_id, body.resolved,
                                                user.tenant_id, user.username))


@router.delete("/annotations/{ann_id}")
async def delete_annotation_api(
    ann_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_ANNOTATE)),
):
    return success(await ann.delete_annotation(
        db, ann_id, user.tenant_id, user.username, is_admin=user.role == "admin"))


@router.post("/annotations/{ann_id}/to-issue")
async def annotation_to_issue_api(
    ann_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_SIGNOFF)),
):
    """批注转治理 issue（协作讨论 → 治理工单，进知识治理页处置）。"""
    return success(await ann.to_governance_issue(db, ann_id, user.tenant_id,
                                                 user.username))


@router.get("/documents/{doc_id}/annotation-stats")
async def annotation_stats_api(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_READ)),
):
    return success(await ann.annotation_stats(db, doc_id, user.tenant_id))


# ---------- 电子签批 ----------

@router.get("/signers")
async def suggest_signers_api(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_SIGNOFF)),
):
    return success(await signoff.suggest_signers(db, user.tenant_id))


@router.post("/documents/{doc_id}/signoffs")
async def create_signoff_api(
    doc_id: str,
    body: SignoffCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_SIGNOFF)),
):
    return success(await signoff.create_signoff(
        db, doc_id, body.title,
        [{"signer": n.signer, "role": n.role} for n in body.signers],
        user.tenant_id, user.username, user.dept, user.role))


@router.get("/documents/{doc_id}/signoffs")
async def list_signoffs_api(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_READ)),
):
    return success(await signoff.list_signoffs(db, doc_id, user.tenant_id,
                                               user.dept, user.role))


@router.get("/signoffs/my-pending")
async def my_pending_api(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_READ)),
):
    return success(await signoff.my_pending(db, user.tenant_id, user.username))


@router.get("/signoffs/{signoff_id}")
async def get_signoff_api(
    signoff_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_READ)),
):
    return success(await signoff.get_signoff_detail(
        db, signoff_id, user.tenant_id, user.dept, user.role))


@router.post("/signoffs/{signoff_id}/submit")
async def submit_signoff_api(
    signoff_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_SIGNOFF)),
):
    return success(await signoff.submit_signoff(db, signoff_id,
                                                user.tenant_id, user.username))


@router.post("/signoffs/{signoff_id}/sign")
async def sign_signoff_api(
    signoff_id: str,
    body: SignoffAction,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_READ)),
):
    # 签批人本身可能任意角色（由会签流指定），权限点在"是否当前节点签批人"+口令复核
    return success(await signoff.sign(db, signoff_id, user.tenant_id,
                                      user, body.password, body.comment))


@router.post("/signoffs/{signoff_id}/reject")
async def reject_signoff_api(
    signoff_id: str,
    body: SignoffAction,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_READ)),
):
    return success(await signoff.reject(db, signoff_id, user.tenant_id,
                                        user, body.password, body.reason))


@router.post("/signoffs/{signoff_id}/cancel")
async def cancel_signoff_api(
    signoff_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_SIGNOFF)),
):
    return success(await signoff.cancel_signoff(
        db, signoff_id, user.tenant_id, user.username, is_admin=user.role == "admin"))


@router.get("/signoffs/{signoff_id}/verify")
async def verify_signoff_api(
    signoff_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_READ)),
):
    return success(await signoff.verify_signoff(db, signoff_id, user.tenant_id))
