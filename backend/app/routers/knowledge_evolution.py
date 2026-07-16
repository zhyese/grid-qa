"""知识库自进化闭环 API。复刻 knowledge_governance 的 scan 入队 + review 范式。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import DOC_MANAGE, DOC_READ
from app.core.response import BizError, success
from app.db.session import get_db
from app.dependencies import require_perm
from app.models.user import User
from app.schemas.knowledge_evolution import EvolutionScanRequest, DraftReviewRequest, DraftWithdrawRequest
from app.services import knowledge_evolution_service as ev
from app.services import task_queue_service

router = APIRouter(prefix="/knowledge-evolution", tags=["知识自进化"])


@router.post("/scan")
async def scan(body: EvolutionScanRequest, db: AsyncSession = Depends(get_db),
               user: User = Depends(require_perm(DOC_MANAGE))):
    queued = await ev.enqueue_evolution_scan(
        user.tenant_id, since_hours=body.sinceHours, model_type=body.modelType)
    return success({"mode": "queued", "task": queued}, "自进化扫描已入队")


@router.get("/scan/{task_id}")
async def scan_status(task_id: str, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_perm(DOC_MANAGE))):
    t = await task_queue_service.get_task(db, task_id, tenant_id=user.tenant_id)
    if not t or t.task_type != ev.TASK_TYPE:
        raise BizError("扫描任务不存在", 404)
    d = task_queue_service.task_to_dict(t)
    d["done"] = t.status in {"succeeded", "failed", "dead"}
    return success(d, "查询成功")


@router.get("/drafts")
async def list_drafts(status: str = Query("", pattern="^(|draft|approved|indexed|rejected|withdrawn)$"),
                      page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                      db: AsyncSession = Depends(get_db), user: User = Depends(require_perm(DOC_READ))):
    return success(await ev.list_drafts(db, user.tenant_id, status=status, page=page, size=size), "查询成功")


@router.get("/drafts/{draft_id}")
async def draft_detail(draft_id: str, db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_perm(DOC_READ))):
    d = await ev.get_draft(db, draft_id, user.tenant_id)
    if not d:
        raise BizError("草稿不存在", 404)
    return success(d, "查询成功")


@router.post("/drafts/{draft_id}/review")
async def draft_review(draft_id: str, body: DraftReviewRequest, db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_perm(DOC_MANAGE))):
    try:
        data = await ev.review_draft(
            db, draft_id, user.tenant_id, action=body.action, note=body.note, reviewer=user.username)
    except ValueError as e:
        raise BizError(str(e), 400)
    return success(data, "已处理")


@router.post("/drafts/{draft_id}/withdraw")
async def draft_withdraw(draft_id: str, body: DraftWithdrawRequest, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_perm(DOC_MANAGE))):
    try:
        data = await ev.withdraw_draft(db, draft_id, user.tenant_id)
    except ValueError as e:
        raise BizError(str(e), 400)
    return success(data, "已撤回")
async def stats(db: AsyncSession = Depends(get_db), user: User = Depends(require_perm(DOC_READ))):
    return success(await ev.get_stats(db, user.tenant_id), "查询成功")
