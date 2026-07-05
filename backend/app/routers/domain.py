"""领域增强接口：故障诊断推理(D1) / 相似历史案例(D2) / 两票辅助生成(D3)。"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limiter import limiter
from app.core.response import success
from app.db.session import get_db
from app.dependencies import get_current_user, require_admin
from app.models.user import User
from app.schemas.domain import (
    DiagnoseAgentRequest, DiagnoseDebateRequest, DiagnoseRequest,
    QueryPlanRequest, SimilarCaseRequest,
    TicketAuditRequest, TicketCreateRequest, TicketExecuteRequest, TicketListRequest, TicketRequest, TicketReviewRequest,
)
from app.services import debate_agent_service
from app.services import diagnose_agent_service
from app.services import domain_service
from app.services import ticket_audit_service
from app.services import ticket_lifecycle_service
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


@router.post("/diagnose-agent")
@limiter.limit("6/minute")
async def diagnose_agent(
    request: Request,
    body: DiagnoseAgentRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Agentic 深度诊断：LLM 自主多轮调工具（检索/图谱/案例/两票）交叉验证，返回诊断 + 思考链 steps。"""
    data = await diagnose_agent_service.diagnose_agent(db, body.symptom, body.modelType)
    await write_log(db, user.username, "深度诊断", f"症状：{body.symptom[:40]}")
    return success(data, "深度诊断完成")


@router.post("/diagnose-debate")
@limiter.limit("4/minute")
async def diagnose_debate(
    request: Request,
    body: DiagnoseDebateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Multi-Agent 辩论式诊断：3角色（规程/图谱/案例）独立诊断→辩论→终裁，返回专家意见+裁决过程。"""
    data = await debate_agent_service.debate_diagnose(db, body.symptom, body.modelType)
    await write_log(db, user.username, "辩论诊断", f"症状：{body.symptom[:40]}")
    return success(data, "辩论诊断完成")


# ===== 两票全生命周期管理 =====


@router.post("/ticket/create")
@limiter.limit("10/minute")
async def ticket_create(
    request: Request,
    body: TicketCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建两票草稿。"""
    data = await ticket_lifecycle_service.create_ticket(
        db, ticket_type=body.ticketType, task=body.task, device=body.device,
        location=body.location, steps=body.steps, safety=body.safety,
        risks=body.risks, notes=body.notes, creator=user.username,
        tenant=user.tenant_id,
    )
    await write_log(db, user.username, "创建两票", f"{body.ticketType}：{body.task[:40]}")
    return success(data, "创建成功")


@router.get("/ticket/list")
async def ticket_list(
    status: str = "", ticketType: str = "", creator: str = "",
    page: int = 1, size: int = 20,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """两票列表。"""
    data = await ticket_lifecycle_service.list_tickets(
        db, status=status, ticket_type=ticketType, creator=creator,
        page=page, size=size, tenant=user.tenant_id,
    )
    return success(data, "查询成功")


@router.get("/ticket/{ticket_id}")
async def ticket_detail(
    ticket_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """票据详情。"""
    data = await ticket_lifecycle_service.get_ticket(db, ticket_id)
    if not data:
        from app.core.response import BizError
        raise BizError("票据不存在", 404)
    return success(data, "查询成功")


@router.post("/ticket/{ticket_id}/submit")
@limiter.limit("10/minute")
async def ticket_submit(
    request: Request,
    ticket_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """提交审核（自动跑审核打分）。"""
    data = await ticket_lifecycle_service.submit_for_review(db, ticket_id)
    await write_log(db, user.username, "两票提交审核", f"票据{ticket_id}")
    return success(data, "提交成功")


@router.post("/ticket/{ticket_id}/review")
@limiter.limit("10/minute")
async def ticket_review(
    request: Request,
    ticket_id: str,
    body: TicketReviewRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """审核票据（通过/驳回）。"""
    data = await ticket_lifecycle_service.review_ticket(
        db, ticket_id, body.approved, body.comment, reviewer=user.username,
    )
    await write_log(db, user.username, "两票审核", f"票据{ticket_id} {'通过' if body.approved else '驳回'}")
    return success(data, "审核完成")


@router.post("/ticket/{ticket_id}/issue")
@limiter.limit("10/minute")
async def ticket_issue(
    request: Request,
    ticket_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """签发票据（审核通过后签发）。"""
    data = await ticket_lifecycle_service.issue_ticket(db, ticket_id, issuer=user.username)
    await write_log(db, user.username, "两票签发", f"票据{ticket_id}")
    return success(data, "签发成功")


@router.post("/ticket/{ticket_id}/execute")
@limiter.limit("10/minute")
async def ticket_execute(
    request: Request,
    ticket_id: str,
    body: TicketExecuteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """开始执行/完成执行票据。没有 executor 参数时开始执行，有 log 时完成执行。"""
    if body.log:
        data = await ticket_lifecycle_service.complete_execution(
            db, ticket_id, log=body.log, deviation=body.deviation,
        )
        await write_log(db, user.username, "两票执行完成", f"票据{ticket_id}")
    else:
        data = await ticket_lifecycle_service.start_execution(
            db, ticket_id, executor=user.username, supervisor=body.supervisor,
        )
        await write_log(db, user.username, "两票开始执行", f"票据{ticket_id}")
    return success(data, "操作成功")


@router.post("/ticket/{ticket_id}/archive")
@limiter.limit("10/minute")
async def ticket_archive(
    request: Request,
    ticket_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """归档票据。"""
    data = await ticket_lifecycle_service.archive_ticket(db, ticket_id)
    await write_log(db, user.username, "两票归档", f"票据{ticket_id}")
    return success(data, "归档成功")


@router.delete("/ticket/{ticket_id}")
async def ticket_delete(
    ticket_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除票据（软删）。"""
    ok = await ticket_lifecycle_service.delete_ticket(db, ticket_id)
    return success({"deleted": ok}, "删除成功" if ok else "票据不存在")


@router.get("/ticket-stats")
async def ticket_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """两票统计看板。"""
    data = await ticket_lifecycle_service.get_ticket_stats(db, tenant=user.tenant_id)
    return success(data, "查询成功")


@router.post("/query-plan")
@limiter.limit("10/minute")
async def query_plan(
    request: Request,
    body: QueryPlanRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """复杂问题分解：问题→子查询DAG→并行检索→综合答案（多步推理/对比/条件判断）。"""
    from app.services.query_plan_service import plan_and_answer
    data = await plan_and_answer(db, body.question, body.modelType)
    await write_log(db, user.username, "复杂问题分解", f"问题：{body.question[:40]}")
    return success(data, "回答成功")
