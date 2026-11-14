"""Agentic 历史快照：原子保存，读取时重新检查归属、租户和来源权限。"""
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import BizError
from app.models.agentic_answer import AgenticAnswer
from app.models.conversation import Conversation, Message
from app.services.agentic_evidence_service import EvidenceReader

_SUMMARY_FIELDS = (
    "status", "stopReason", "missingEvidence", "conflicts", "rounds", "toolCalls", "verification",
)
_STEP_FIELDS = ("iter", "phase", "tool", "result", "error")


async def _accessible(db, conversation_id, username, tenant, *, lock=False):
    if not username or not isinstance(tenant, str) or not tenant.strip():
        return False
    stmt = select(Conversation.id).where(
        Conversation.id == conversation_id, Conversation.username == username,
        Conversation.is_deleted.is_(False),
    )
    if lock:
        stmt = stmt.with_for_update()
    if (await db.execute(stmt)).scalar_one_or_none() is None:
        return False
    # 旧会话无租户列；只要存在其他租户的快照，普通Message中的答案也不能返回。
    foreign = await db.execute(select(AgenticAnswer.message_id).where(
        AgenticAnswer.conversation_id == conversation_id,
        or_(AgenticAnswer.tenant_id != tenant, AgenticAnswer.username != username),
    ).limit(1))
    return foreign.scalar_one_or_none() is None


async def save_answer(db: AsyncSession, conversation_id: str, query: str, result: dict,
                      *, username: str, tenant: str) -> None:
    """两条消息与结构化快照共用一次提交；失败时不留下不完整的历史。"""
    try:
        if not await _accessible(db, conversation_id, username, tenant, lock=True):
            raise BizError("会话不存在或不可访问", code=404)
        payload = deepcopy({
            "sources": [{**source, "citationIndex": index + 1}
                        for index, source in enumerate(result.get("sources", []))],
            "evidencePack": result.get("evidencePack", []),
            "agenticResult": {key: result[key] for key in _SUMMARY_FIELDS if key in result},
            "agentSteps": [{key: step[key] for key in _STEP_FIELDS if key in step}
                           for step in result.get("steps", []) if isinstance(step, dict)],
            "confidence": result.get("confidence", "refused"),
            "degraded": bool(result.get("degraded", False)),
        })
        # messages旧路径使用DB NOW()；容器UTC与MySQL本地时区不能混用。
        now = (await db.execute(select(func.now()))).scalar_one()
        # MySQL旧DateTime列精度为秒；带时间前缀的ID用于同秒消息的稳定排序。
        pair_id = now.strftime("%Y%m%d%H%M%S") + f"{datetime.now().microsecond:06d}" + uuid4().hex[:12]
        question = Message(id=pair_id + "0", conversation_id=conversation_id,
                           role="user", content=query, created_at=now)
        answer = Message(id=pair_id + "1", conversation_id=conversation_id,
                         role="assistant", content=result["answer"], created_at=now)
        db.add_all([question, answer])
        await db.flush()
        db.add(AgenticAnswer(message_id=answer.id, conversation_id=conversation_id,
                             username=username, tenant_id=tenant, payload=payload))
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


async def _snapshot(reader, message, payload):
    data = deepcopy(payload)
    original = data.get("evidencePack", [])
    # 新reader没有签发过旧遥测包，不能把当时的统计按当前now重跑或冒充实时数据。
    candidates = [item for item in original if item.get("kind") in {"document", "graph"}]
    valid = await reader.revalidate(candidates) if candidates else []
    valid_ids = {item.get("evidenceId") for item in valid if item.get("evidenceId")}
    sources = data.get("sources", [])
    data["sources"] = [source for source in sources if source.get("evidenceId") in valid_ids]
    data["evidencePack"] = valid
    warnings = ["历史快照：未重新执行模型推理，不代表当前结论。"]
    if any(item.get("kind") == "telemetry" for item in original):
        warnings.append("遥测为当时的非实时历史快照，当前无法完整复核，已隐藏旧遥测来源及原结论。")
    if len(valid) != len(original) or len(data["sources"]) != len(sources):
        warnings.append("部分来源已变化、权限已撤回或无法复核；原结论与相关过程文本已隐藏。")
        # 原答案/冲突/工具结果可能直接引用撤权原文，不能只过滤sources。
        message["content"] = "历史回答的来源已变化或不可访问，原结论暂不展示，请重新查证。"
        summary = data.setdefault("agenticResult", {})
        summary.update(status="partial" if valid else "insufficient", stopReason="source_changed",
                       missingEvidence=[], conflicts=[], verification={})
        data["agentSteps"] = [{key: step[key] for key in ("iter", "phase", "tool", "error") if key in step}
                              for step in data.get("agentSteps", [])]
        data["confidence"] = "refused"
        data["degraded"] = True
    return {**message, **data, "agenticRag": True, "agentMode": True,
            "historySnapshot": True, "historyWarnings": warnings}


async def history(db: AsyncSession, conversation_id: str, *, username: str, tenant: str,
                  role: str = "", dept: str = "", limit: int = 100) -> list[dict]:
    if not await _accessible(db, conversation_id, username, tenant):
        return []
    rows = (await db.execute(select(Message).where(
        Message.conversation_id == conversation_id, Message.is_deleted.is_(False),
    ).order_by(Message.created_at.desc(), Message.id.desc()).limit(max(0, min(limit, 100))))).scalars().all()
    if not rows:
        return []
    snapshots = (await db.execute(select(AgenticAnswer).where(
        AgenticAnswer.message_id.in_([row.id for row in rows]),
        AgenticAnswer.conversation_id == conversation_id,
        AgenticAnswer.username == username, AgenticAnswer.tenant_id == tenant,
    ))).scalars().all()
    by_message = {row.message_id: row.payload for row in snapshots}
    reader = EvidenceReader(db, tenant=tenant, role=role, dept=dept)
    messages = []
    for row in reversed(rows):
        message = {"id": row.id, "role": row.role, "content": row.content}
        if row.id in by_message:
            message = await _snapshot(reader, message, by_message[row.id])
        messages.append(message)
    return messages
