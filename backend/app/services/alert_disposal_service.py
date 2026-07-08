"""告警自动处置：写 pending → bg task 跑 ALERT_PERSONA → 更新 disposed。

仿 rewrite_event_service：bg task 用独立 AsyncSessionLocal。
"""
import json

from sqlalchemy import desc, func, select

from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.alert_disposal import AlertDisposal


async def trigger_disposal(severity: str, title: str, summary: str,
                           source: str = "manual", model_type: str | None = None) -> int:
    """写 pending 记录 → 启动 bg task 跑 ALERT_PERSONA → 返回记录 id。

    调用方 await（写 pending 快）；disposal 跑在 bg task，不阻塞调用方。
    """
    import asyncio
    async with AsyncSessionLocal() as db:
        rec = AlertDisposal(
            severity=severity or "warning", title=(title or "")[:256],
            summary=(summary or "")[:2000], status="pending", source=source,
        )
        db.add(rec)
        await db.commit()
        await db.refresh(rec)
        disp_id = rec.id
    asyncio.create_task(_run_disposal(disp_id, summary or title or "", model_type))
    return disp_id


async def _run_disposal(disp_id: int, alert_text: str, model_type: str | None) -> None:
    """bg task：跑 ALERT_PERSONA → 拆 diagnosis/handling/ticket → 更新 disposed。"""
    try:
        from app.services.agent_runtime import run_agent
        from app.services.agent_personas import ALERT_PERSONA
        res = await run_agent(None, ALERT_PERSONA, f"告警：{alert_text}", model_type)
        ans = res.answer if isinstance(res.answer, dict) else {"summary": str(res.answer)[:500]}
        handling = ans.get("summary", "") if isinstance(ans, dict) else ""
        ticket = ans.get("ticket") or {} if isinstance(ans, dict) else {}
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(AlertDisposal).where(AlertDisposal.id == disp_id)
            )).scalar_one_or_none()
            if row:
                row.diagnosis_json = json.dumps(ans, ensure_ascii=False)[:8000]
                row.handling = (handling or "")[:2000]
                row.ticket_draft_json = json.dumps(ticket, ensure_ascii=False)[:4000]
                row.status = "disposed"
                await db.commit()
    except Exception as e:
        degraded("alert_disposal_run", e)
        try:
            async with AsyncSessionLocal() as db:
                row = (await db.execute(
                    select(AlertDisposal).where(AlertDisposal.id == disp_id)
                )).scalar_one_or_none()
                if row:
                    row.status = "disposed"
                    row.handling = f"自动处置失败: {type(e).__name__}: {e}"[:2000]
                    await db.commit()
        except Exception:
            pass


async def list_disposals(page: int = 1, size: int = 20, status: str | None = None) -> dict:
    """分页查询告警处置记录（admin 用）。"""
    try:
        async with AsyncSessionLocal() as db:
            base = select(AlertDisposal)
            cnt = select(func.count()).select_from(AlertDisposal)
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
            } for r in rows]}
    except Exception as e:
        degraded("alert_disposal_list", e)
        return {"total": 0, "list": []}
