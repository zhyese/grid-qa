"""领域增强接口：故障诊断推理(D1) / 相似历史案例(D2) / 两票辅助生成(D3)。"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limiter import limiter
from app.core.response import success
from app.db.session import get_db
from app.dependencies import get_current_user, require_admin
from app.models.user import User
from app.schemas.domain import DiagnoseRequest, SimilarCaseRequest, TicketAuditRequest, TicketRequest
from app.services import domain_service
from app.services import ticket_audit_service
from app.services.log_service import write_log

router = APIRouter(prefix="/domain", tags=["领域增强"])


@router.post("/diagnose")
@limiter.limit("10/minute")
async def diagnose(
    request: Request,
    body: DiagnoseRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """故障诊断推理：症状→可能原因排序+处置+风险（复用检索+图谱多查询分解）。"""
    data = await domain_service.diagnose(db, body.symptom, body.modelType)
    await write_log(db, user.username, "故障诊断", f"症状：{body.symptom[:40]}")
    return success(data, "诊断完成")


@router.post("/similar-case")
@limiter.limit("20/minute")
async def similar_case(
    request: Request,
    body: SimilarCaseRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """相似历史故障案例检索（限定故障案例库，"历史上类似怎么处理的"）。"""
    data = await domain_service.similar_case(db, body.symptom, body.modelType)
    return success(data, "查询成功")


@router.post("/ticket")
@limiter.limit("10/minute")
async def ticket(
    request: Request,
    body: TicketRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """两票辅助生成：操作任务→结构化操作票（步骤/安全措施/风险点）。"""
    data = await domain_service.generate_ticket(db, body.task, body.modelType)
    await write_log(db, user.username, "两票生成", f"任务：{body.task[:40]}")
    return success(data, "生成完成")


@router.post("/ticket/audit")
@limiter.limit("10/minute")
async def ticket_audit(
    request: Request,
    body: TicketAuditRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """两票智能审核：已填票据 → 规则引擎+LLM 双层审核报告（仅管理员）。"""
    data = await ticket_audit_service.audit_ticket(body.ticketText, body.ticketType, body.modelType)
    await write_log(db, user.username, "两票审核", f"{body.ticketType} | {body.ticketText[:40]}")
    return success(data, "审核完成")
