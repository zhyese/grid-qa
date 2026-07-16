"""告警自动处置：写 pending → 持久化任务跑 ALERT_PERSONA → 更新审核态。"""
import json
from datetime import datetime

from sqlalchemy import desc, func, select

from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.alert_disposal import AlertDisposal


TASK_TYPE = "alert_disposal.process"


def _tenant(value: str | None) -> str:
    return (value or "default").strip() or "default"


async def trigger_disposal(severity: str, title: str, summary: str,
                           source: str = "manual", model_type: str | None = None,
                           tenant_id: str = "default") -> int:
    """写 pending 记录并与持久化任务同事务提交，返回记录 id。

    任务队列的 tenant_id 与业务记录一致，worker 只能处理该租户记录。
    """
    tenant_id = _tenant(tenant_id)
    async with AsyncSessionLocal() as db:
        rec = AlertDisposal(
            tenant_id=tenant_id,
            severity=severity or "warning", title=(title or "")[:256],
            summary=(summary or "")[:2000], status="pending", source=source,
        )
        db.add(rec)
        await db.flush()
        from app.services.task_queue_service import enqueue_task_record

        await enqueue_task_record(
            db,
            TASK_TYPE,
            {
                "disposal_id": rec.id,
                "alert_text": summary or title or "",
                "model_type": model_type,
            },
            queue="realtime",
            idempotency_key=f"alert-disposal:{rec.id}",
            tenant_id=tenant_id,
            max_attempts=3,
            commit=False,
        )
        await db.commit()
        await db.refresh(rec)
        disp_id = rec.id
    return disp_id


async def _run_disposal(disp_id: int, alert_text: str, model_type: str | None,
                        tenant_id: str = "default") -> dict:
    """持久化任务：跑 ALERT_PERSONA → 拆 diagnosis/handling/ticket → 更新 proposed。"""
    tenant_id = _tenant(tenant_id)
    try:
        from app.services.agent_runtime import run_agent
        from app.services.persona_store import get_persona
        alert_persona = await get_persona("alert")
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(AlertDisposal).where(
                    AlertDisposal.id == disp_id,
                    AlertDisposal.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
            if not row:
                raise ValueError("处置记录不存在")
            res = await run_agent(
                db,
                alert_persona,
                f"告警：{alert_text}",
                model_type,
                ctx={"username": "system", "tenant": tenant_id, "role": "admin"},
            )
        ans = res.answer if isinstance(res.answer, dict) else {"summary": str(res.answer)[:500]}
        handling = ans.get("summary", "") if isinstance(ans, dict) else ""
        ticket = ans.get("ticket") or {} if isinstance(ans, dict) else {}
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(AlertDisposal).where(
                    AlertDisposal.id == disp_id,
                    AlertDisposal.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
            if row:
                row.diagnosis_json = json.dumps(ans, ensure_ascii=False)[:8000]
                row.handling = (handling or "")[:2000]
                row.ticket_draft_json = json.dumps(ticket, ensure_ascii=False)[:4000]
                row.status = "proposed"
                await db.commit()
                return {"id": disp_id, "status": row.status}
    except Exception as e:
        degraded("alert_disposal_run", e)
        try:
            async with AsyncSessionLocal() as db:
                row = (await db.execute(
                    select(AlertDisposal).where(
                        AlertDisposal.id == disp_id,
                        AlertDisposal.tenant_id == tenant_id,
                    )
                )).scalar_one_or_none()
                if row:
                    row.status = "proposed"
                    row.handling = f"自动处置失败: {type(e).__name__}: {e}"[:2000]
                    await db.commit()
                    return {"id": disp_id, "status": row.status, "degraded": True}
        except Exception:
            pass
    return {"id": disp_id, "status": "missing", "degraded": True}


async def list_disposals(page: int = 1, size: int = 20, status: str | None = None,
                         tenant_id: str = "default") -> dict:
    """分页查询当前租户的告警处置记录。"""
    tenant_id = _tenant(tenant_id)
    try:
        async with AsyncSessionLocal() as db:
            base = select(AlertDisposal).where(AlertDisposal.tenant_id == tenant_id)
            cnt = select(func.count()).select_from(AlertDisposal).where(
                AlertDisposal.tenant_id == tenant_id
            )
            if status:
                base = base.where(AlertDisposal.status == status)
                cnt = cnt.where(AlertDisposal.status == status)
            total = (await db.execute(cnt)).scalar() or 0
            rows = (await db.execute(
                base.order_by(desc(AlertDisposal.created_at)).offset((page - 1) * size).limit(size)
            )).scalars().all()
            return {"total": total, "list": [{
                "id": r.id,
                "createdAt": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                "severity": r.severity, "title": r.title, "summary": r.summary,
                "diagnosis": r.diagnosis_json, "handling": r.handling,
                "ticketDraft": r.ticket_draft_json, "status": r.status, "source": r.source,
                "tenantId": r.tenant_id,
                "ticketId": r.ticket_id, "reviewer": r.reviewer, "reviewNote": r.review_note,
                "reviewedAt": r.reviewed_at.strftime("%Y-%m-%d %H:%M:%S") if r.reviewed_at else "",
            } for r in rows]}
    except Exception as e:
        degraded("alert_disposal_list", e)
        return {"total": 0, "list": []}


# ===== ③增强：人工确认闭环 + 一键转两票 =====

def _to_dict(r: AlertDisposal) -> dict:
    return {
        "id": r.id, "severity": r.severity, "title": r.title, "summary": r.summary,
        "handling": r.handling, "status": r.status, "source": r.source,
        "tenantId": r.tenant_id,
        "ticketId": r.ticket_id, "reviewer": r.reviewer, "reviewNote": r.review_note,
        "reviewedAt": r.reviewed_at.strftime("%Y-%m-%d %H:%M:%S") if r.reviewed_at else "",
    }


async def confirm_disposal(db, disp_id: int, reviewer: str,
                           tenant_id: str = "default") -> dict:
    """admin 采纳处置预案：proposed → confirmed（disposed 兼容旧库存）。"""
    row = (await db.execute(select(AlertDisposal).where(
        AlertDisposal.id == disp_id,
        AlertDisposal.tenant_id == _tenant(tenant_id),
    ).with_for_update())).scalar_one_or_none()
    if not row:
        raise ValueError("处置记录不存在")
    if row.status not in ("proposed", "disposed"):
        raise ValueError(f"当前状态 {row.status} 不可确认（需 proposed）")
    row.status = "confirmed"
    row.reviewer = reviewer
    row.reviewed_at = datetime.now()
    await db.commit()
    await db.refresh(row)
    return _to_dict(row)


async def reject_disposal(db, disp_id: int, reviewer: str, note: str = "",
                          tenant_id: str = "default") -> dict:
    """admin 驳回：→ rejected。"""
    row = (await db.execute(select(AlertDisposal).where(
        AlertDisposal.id == disp_id,
        AlertDisposal.tenant_id == _tenant(tenant_id),
    ).with_for_update())).scalar_one_or_none()
    if not row:
        raise ValueError("处置记录不存在")
    if row.status not in ("proposed", "disposed", "confirmed"):
        raise ValueError(f"当前状态 {row.status} 不可驳回")
    row.status = "rejected"
    row.reviewer = reviewer
    row.review_note = (note or "")[:500]
    row.reviewed_at = datetime.now()
    await db.commit()
    await db.refresh(row)
    return _to_dict(row)


async def to_ticket(db, disp_id: int, creator: str = "system", tenant: str = "default") -> dict:
    """已确认预案 → 创建两票草稿（不自动提交审核，留给人工走 submit_for_review）：confirmed → ticketed。"""
    from app.services import ticket_lifecycle_service
    tenant = _tenant(tenant)
    row = (await db.execute(select(AlertDisposal).where(
        AlertDisposal.id == disp_id,
        AlertDisposal.tenant_id == tenant,
    ).with_for_update())).scalar_one_or_none()
    if not row:
        raise ValueError("处置记录不存在")
    if row.status != "confirmed":
        raise ValueError("仅已确认(confirmed)预案可转两票")
    try:
        draft = json.loads(row.ticket_draft_json) if row.ticket_draft_json else {}
    except Exception:
        draft = {}
    ticket = await ticket_lifecycle_service.create_ticket(
        db,
        ticket_type=draft.get("ticket_type") or draft.get("ticketType") or "操作票",
        task=draft.get("task") or row.title or "告警处置",
        device=draft.get("device", ""), location=draft.get("location", ""),
        steps=draft.get("steps") or [],
        safety=draft.get("safety") or draft.get("safety_measures") or [],
        risks=draft.get("risks") or [],
        notes=(draft.get("notes") or f"来源告警：{row.title}"),
        creator=creator, tenant=tenant,
        source_ref=f"alert-disposal:{row.id}", commit=False,
    )
    row.ticket_id = ticket.get("id") or ticket.get("ticketId") or ""
    row.status = "ticketed"
    await db.commit()
    await db.refresh(row)
    return {"disposal": _to_dict(row), "ticket": ticket}


async def close_disposal(db, disp_id: int, reviewer: str, note: str = "",
                         tenant_id: str = "default") -> dict:
    """关闭处置（误报/已手动处理，无需两票）。"""
    row = (await db.execute(select(AlertDisposal).where(
        AlertDisposal.id == disp_id,
        AlertDisposal.tenant_id == _tenant(tenant_id),
    ).with_for_update())).scalar_one_or_none()
    if not row:
        raise ValueError("处置记录不存在")
    if row.status in ("ticketed", "closed"):
        raise ValueError(f"当前状态 {row.status} 不可关闭")
    row.status = "closed"
    row.reviewer = reviewer
    row.review_note = (note or "")[:500]
    row.reviewed_at = datetime.now()
    await db.commit()
    await db.refresh(row)
    return _to_dict(row)


async def alert_disposal_task_handler(payload: dict, context) -> dict:
    """持久化任务 handler；租户信息只信任 worker 上下文。"""
    result = await _run_disposal(
        int(payload["disposal_id"]),
        str(payload.get("alert_text") or ""),
        payload.get("model_type"),
        tenant_id=context.tenant_id,
    )
    if result.get("degraded"):
        # 交给持久任务中心统一退避和死信；业务记录已保留可读错误说明。
        raise RuntimeError(f"告警自动处置未完成: {result.get('status', 'unknown')}")
    return result


try:
    from app.tasks.registry import register_task_handler

    register_task_handler(TASK_TYPE, alert_disposal_task_handler)
except ImportError:
    # 兼容未安装任务中心的最小运行环境。
    pass
