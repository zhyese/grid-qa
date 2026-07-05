"""两票全生命周期管理：草稿→审核→签发→执行→归档。

与现有 ticket_audit_service.py 的关系：
- ticket_audit_service：审核打分子系统（规则+LLM双层），返回 score/items
- ticket_lifecycle_service：工作流编排，调用 audit 做审核步骤
"""
import json
import uuid
from datetime import datetime

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.obs import degraded
from app.models.ticket import Ticket, TicketStatus, TicketType
from app.services import ticket_audit_service


def _now():
    return datetime.now()


async def create_ticket(
    db: AsyncSession, *,
    ticket_type: str = "操作票", task: str = "", device: str = "",
    location: str = "", steps: list[str] | None = None,
    safety: list[str] | None = None, risks: list[str] | None = None,
    notes: str = "", creator: str = "", tenant: str = "default",
) -> dict:
    """创建新票据（草稿状态）。"""
    ticket = Ticket(
        id=uuid.uuid4().hex,
        tenant_id=tenant,
        ticket_type=TicketType(ticket_type),
        status=TicketStatus.DRAFT,
        title=task[:200] if task else "两票",
        task=task,
        device=device,
        location=location,
        steps=json.dumps(steps or [], ensure_ascii=False),
        safety_measures=json.dumps(safety or [], ensure_ascii=False),
        risks=json.dumps(risks or [], ensure_ascii=False),
        notes=notes,
        creator=creator,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)   # 显式回填 server_default 字段，避免 _ticket_to_dict 触发 lazy load → MissingGreenlet
    return _ticket_to_dict(ticket)


async def get_ticket(db: AsyncSession, ticket_id: str) -> dict | None:
    """查询单张票据。"""
    t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id, Ticket.is_deleted == 0))).scalar_one_or_none()
    return _ticket_to_dict(t) if t else None


async def list_tickets(
    db: AsyncSession, status: str = "", ticket_type: str = "",
    creator: str = "", page: int = 1, size: int = 20,
    tenant: str = "default",
) -> dict:
    """票据列表（支持状态/类型/创建人过滤）。"""
    stmt = select(Ticket).where(Ticket.is_deleted == 0, Ticket.tenant_id == tenant)
    cnt = select(func.count()).select_from(Ticket).where(Ticket.is_deleted == 0, Ticket.tenant_id == tenant)
    if status:
        stmt = stmt.where(Ticket.status == TicketStatus(status))
        cnt = cnt.where(Ticket.status == TicketStatus(status))
    if ticket_type:
        stmt = stmt.where(Ticket.ticket_type == TicketType(ticket_type))
        cnt = cnt.where(Ticket.ticket_type == TicketType(ticket_type))
    if creator:
        stmt = stmt.where(Ticket.creator == creator)
        cnt = cnt.where(Ticket.creator == creator)
    total = (await db.execute(cnt)).scalar() or 0
    rows = (await db.execute(
        stmt.order_by(desc(Ticket.created_at)).offset((page - 1) * size).limit(size)
    )).scalars().all()
    return {"total": total, "list": [_ticket_to_dict(r) for r in rows]}


async def update_ticket_content(
    db: AsyncSession, ticket_id: str, **kwargs,
) -> dict | None:
    """修改票据内容（仅 draft 状态可改）。"""
    t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id, Ticket.is_deleted == 0))).scalar_one_or_none()
    if not t:
        return None
    if t.status != TicketStatus.DRAFT:
        raise ValueError("仅草稿状态可修改内容")
    for k, v in kwargs.items():
        if hasattr(t, k):
            if k in ("steps", "safety_measures", "risks"):
                setattr(t, k, json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v)
            else:
                setattr(t, k, v)
    await db.commit()
    await db.refresh(t)
    return _ticket_to_dict(t)


async def submit_for_review(db: AsyncSession, ticket_id: str) -> dict:
    """提交审核：draft → pending_review，并自动跑审核。"""
    t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id, Ticket.is_deleted == 0))).scalar_one_or_none()
    if not t:
        raise ValueError("票据不存在")
    if t.status != TicketStatus.DRAFT:
        raise ValueError("仅草稿可提交审核")
    t.status = TicketStatus.PENDING_REVIEW
    t.updated_at = _now()
    # 自动跑审核
    try:
        ticket_text = _build_ticket_text(t)
        audit = await ticket_audit_service.audit_ticket(
            ticket_text, t.ticket_type.value,
        )
        t.audit_report = json.dumps(audit, ensure_ascii=False)
        t.review_score = audit.get("score", 0)
        # 高分自动初审通过
        if audit.get("score", 0) >= 85:
            t.status = TicketStatus.REVIEWED
            t.reviewed_at = _now()
    except Exception as e:
        degraded("ticket_submit_audit", e)
    await db.commit()
    await db.refresh(t)
    return _ticket_to_dict(t)


async def review_ticket(
    db: AsyncSession, ticket_id: str, approved: bool, comment: str = "",
    reviewer: str = "",
) -> dict:
    """审核通过/驳回。"""
    t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id, Ticket.is_deleted == 0))).scalar_one_or_none()
    if not t:
        raise ValueError("票据不存在")
    if t.status not in (TicketStatus.PENDING_REVIEW, TicketStatus.REVIEWED):
        raise ValueError("当前状态不允许审核操作")
    if approved:
        t.status = TicketStatus.REVIEWED
    else:
        t.status = TicketStatus.REJECTED
        t.review_comment = (t.review_comment + "\n" + comment).strip()
    t.reviewer = reviewer or t.reviewer
    t.reviewed_at = _now()
    await db.commit()
    await db.refresh(t)
    return _ticket_to_dict(t)


async def issue_ticket(db: AsyncSession, ticket_id: str, issuer: str = "") -> dict:
    """签发票据：reviewed → issued。"""
    t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id, Ticket.is_deleted == 0))).scalar_one_or_none()
    if not t:
        raise ValueError("票据不存在")
    if t.status != TicketStatus.REVIEWED:
        raise ValueError("仅审核通过的票据可签发")
    t.status = TicketStatus.ISSUED
    t.issuer = issuer or t.issuer
    t.issued_at = _now()
    await db.commit()
    await db.refresh(t)
    return _ticket_to_dict(t)


async def start_execution(
    db: AsyncSession, ticket_id: str, executor: str = "", supervisor: str = "",
) -> dict:
    """开始执行：issued → in_execution。"""
    t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id, Ticket.is_deleted == 0))).scalar_one_or_none()
    if not t:
        raise ValueError("票据不存在")
    if t.status != TicketStatus.ISSUED:
        raise ValueError("仅已签发票据可开始执行")
    t.status = TicketStatus.IN_EXECUTION
    t.executor = executor or t.executor
    t.supervisor = supervisor or t.supervisor
    t.executed_at = _now()
    await db.commit()
    await db.refresh(t)
    return _ticket_to_dict(t)


async def complete_execution(
    db: AsyncSession, ticket_id: str, log: str = "", deviation: str = "",
) -> dict:
    """完成执行：in_execution → completed。"""
    t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id, Ticket.is_deleted == 0))).scalar_one_or_none()
    if not t:
        raise ValueError("票据不存在")
    if t.status != TicketStatus.IN_EXECUTION:
        raise ValueError("仅执行中状态可完成")
    t.status = TicketStatus.COMPLETED
    t.completed_at = _now()
    if log:
        t.execution_log = log[:2000]
    if deviation:
        t.deviation = deviation[:1000]
    await db.commit()
    await db.refresh(t)
    return _ticket_to_dict(t)


async def archive_ticket(db: AsyncSession, ticket_id: str) -> dict:
    """归档票据：completed → archived。"""
    t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id, Ticket.is_deleted == 0))).scalar_one_or_none()
    if not t:
        raise ValueError("票据不存在")
    if t.status != TicketStatus.COMPLETED:
        raise ValueError("仅已完成票据可归档")
    t.status = TicketStatus.ARCHIVED
    t.archived_at = _now()
    await db.commit()
    await db.refresh(t)
    return _ticket_to_dict(t)


async def delete_ticket(db: AsyncSession, ticket_id: str) -> bool:
    """软删票据。"""
    t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id, Ticket.is_deleted == 0))).scalar_one_or_none()
    if not t:
        return False
    t.is_deleted = 1
    await db.commit()
    return True


async def get_ticket_stats(db: AsyncSession, tenant: str = "default") -> dict:
    """票据统计看板。"""
    base = select(Ticket).where(Ticket.is_deleted == 0, Ticket.tenant_id == tenant)
    total = (await db.execute(select(func.count()).select_from(Ticket).where(
        Ticket.is_deleted == 0, Ticket.tenant_id == tenant))).scalar() or 0
    by_status = (await db.execute(
        select(Ticket.status, func.count()).where(
            Ticket.is_deleted == 0, Ticket.tenant_id == tenant,
        ).group_by(Ticket.status)
    )).all()
    by_type = (await db.execute(
        select(Ticket.ticket_type, func.count()).where(
            Ticket.is_deleted == 0, Ticket.tenant_id == tenant,
        ).group_by(Ticket.ticket_type)
    )).all()
    avg_score = (await db.execute(
        select(func.avg(Ticket.review_score)).where(
            Ticket.is_deleted == 0, Ticket.tenant_id == tenant,
            Ticket.review_score > 0,
        )
    )).scalar() or 0

    return {
        "total": total,
        "byStatus": {str(s): c for s, c in by_status},
        "byType": {str(t): c for t, c in by_type},
        "avgReviewScore": round(float(avg_score), 1),
    }


# ---------- 内部辅助 ----------

def _ticket_to_dict(t: Ticket) -> dict:
    return {
        "id": t.id,
        "ticketType": t.ticket_type.value if t.ticket_type else "操作票",
        "status": t.status.value if t.status else "draft",
        "title": t.title,
        "task": t.task,
        "device": t.device,
        "location": t.location,
        "steps": _parse_json(t.steps, []),
        "safety": _parse_json(t.safety_measures, []),
        "risks": _parse_json(t.risks, []),
        "notes": t.notes,
        "creator": t.creator or "",
        "reviewer": t.reviewer or "",
        "issuer": t.issuer or "",
        "executor": t.executor or "",
        "supervisor": t.supervisor or "",
        "reviewScore": t.review_score or 0,
        "reviewComment": t.review_comment or "",
        "auditReport": _parse_json(t.audit_report, {}),
        "executionLog": t.execution_log or "",
        "deviation": t.deviation or "",
        "version": t.version or 1,
        "createdAt": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "",
        "updatedAt": t.updated_at.strftime("%Y-%m-%d %H:%M") if t.updated_at else "",
        "reviewedAt": t.reviewed_at.strftime("%Y-%m-%d %H:%M") if t.reviewed_at else "",
        "issuedAt": t.issued_at.strftime("%Y-%m-%d %H:%M") if t.issued_at else "",
        "completedAt": t.completed_at.strftime("%Y-%m-%d %H:%M") if t.completed_at else "",
    }


def _parse_json(text: str | None, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


def _build_ticket_text(t: Ticket) -> str:
    """把 token 字段拼接为完整票据文本，供审核引擎使用。"""
    steps = _parse_json(t.steps, [])
    safety = _parse_json(t.safety_measures, [])
    risks = _parse_json(t.risks, [])
    lines = [f"任务：{t.task or ''}"]
    for i, s in enumerate(steps, 1):
        lines.append(f"步骤{i}：{s}")
    for s in safety:
        lines.append(f"安全措施：{s}")
    for r in risks:
        lines.append(f"危险点：{r}")
    return "\n".join(lines)