"""知识时效、复审与冲突治理 API。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import DOC_MANAGE, DOC_READ
from app.core.response import BizError, success
from app.db.session import get_db
from app.dependencies import require_perm
from app.models.user import User
from app.schemas.knowledge_governance import (
    GovernanceIssueReviewRequest,
    GovernanceScanRequest,
    KnowledgeMetadataUpsert,
)
from app.services import knowledge_governance_service as governance
from app.services import task_queue_service


router = APIRouter(prefix="/knowledge-governance", tags=["知识治理"])


@router.get("/documents")
async def list_documents(
    keyword: str = Query(default="", max_length=100),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_READ)),
):
    data = await governance.list_documents_with_metadata(
        db, user.tenant_id, keyword=keyword, page=page, size=size,
    )
    return success(data, "查询成功")


@router.get("/documents/{doc_id}/profile")
@router.get("/documents/{doc_id}/metadata", include_in_schema=False)
async def metadata_detail(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_READ)),
):
    return success(
        await governance.get_metadata(db, doc_id, user.tenant_id),
        "查询成功",
    )


@router.put("/documents/{doc_id}/profile")
@router.put("/documents/{doc_id}/metadata", include_in_schema=False)
async def metadata_upsert(
    doc_id: str,
    body: KnowledgeMetadataUpsert,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_MANAGE)),
):
    data = await governance.upsert_metadata(
        db,
        doc_id,
        user.tenant_id,
        body.model_dump(exclude_unset=True),
        user.username,
    )
    return success(data, "治理元数据已保存")


@router.post("/scan")
async def scan(
    body: GovernanceScanRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_MANAGE)),
):
    options = body.model_dump()
    queued = await governance.enqueue_governance_scan(user.tenant_id, **options)
    if queued is not None:
        return success({"mode": "queued", "task": queued}, "治理扫描已进入任务队列")
    data = await governance.run_scan(db, user.tenant_id, **options)
    return success({"mode": "synchronous", **data}, "治理扫描完成")


@router.get("/scan/{task_id}")
async def scan_task_status(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_MANAGE)),
):
    task = await task_queue_service.get_task(db, task_id, tenant_id=user.tenant_id)
    if not task or task.task_type != "knowledge.scan":
        raise BizError("治理扫描任务不存在", 404)
    data = task_queue_service.task_to_dict(task)
    data["done"] = task.status in {"succeeded", "failed", "dead"}
    return success(data, "查询成功")


@router.get("/issues")
async def issue_list(
    status: str = Query(default="", pattern="^(|open|confirmed|resolved|ignored)$"),
    issueType: str = Query(default="", max_length=32),
    severity: str = Query(default="", pattern="^(|info|warning|critical)$"),
    keyword: str = Query(default="", max_length=100),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_MANAGE)),
):
    data = await governance.list_issues(
        db,
        user.tenant_id,
        status=status,
        issue_type=issueType,
        severity=severity,
        keyword=keyword,
        page=page,
        size=size,
    )
    return success(data, "查询成功")


@router.get("/issues/stats")
async def issue_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_MANAGE)),
):
    return success(await governance.get_stats(db, user.tenant_id), "查询成功")


@router.get("/issues/{issue_id}")
async def issue_detail(
    issue_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_MANAGE)),
):
    return success(await governance.get_issue(db, issue_id, user.tenant_id), "查询成功")


@router.post("/issues/{issue_id}/review")
async def issue_review(
    issue_id: str,
    body: GovernanceIssueReviewRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_MANAGE)),
):
    data = await governance.review_issue(
        db,
        issue_id,
        user.tenant_id,
        status=body.status,
        note=body.note,
        reviewer=user.username,
    )
    return success(data, "审核状态已更新")
