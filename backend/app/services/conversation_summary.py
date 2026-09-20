"""P4-⑭ 多轮对话摘要压缩。

多轮对话每 N 轮做一次 LLM 摘要 → 存储为摘要片段。
下次检索时用摘要替代/补全原始历史，节省 token 且保留关键上下文。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.obs import degraded
from app.models.conversation import Conversation
from app.providers.factory import get_llm_provider

_SUMMARIZE_INTERVAL = 6  # 每 6 条消息做一次摘要

_SUMMARY_PROMPT = """你是电网运维对话摘要专家。压缩以下多轮问答记录为一段摘要（100字内），
保留：已讨论的设备/故障、关键诊断结论、已排除的原因、遗留的待确认项。
只输出摘要文本，不要寒暄。

对话记录：
{history}"""


async def summarize_conversation(
    db: AsyncSession, conversation_id: str, messages: list[dict],
    model_type: str | None = None,
) -> str | None:
    """对对话记录做摘要，存入 conversation.summary 字段。"""
    if len(messages) < _SUMMARIZE_INTERVAL:
        return None

    provider = get_llm_provider(model_type)
    history_text = "\n".join(
        f"{'用户' if m.get('role') == 'user' else '助手'}：{(m.get('content') or '')[:200]}"
        for m in messages[-_SUMMARIZE_INTERVAL:]
    )
    try:
        summary = await provider.chat(
            [{"role": "user", "content": _SUMMARY_PROMPT.format(history=history_text)}],
            temperature=0.2, max_tokens=200,
        )
        summary = (summary or "").strip()
        if summary and len(summary) > 20:
            # 更新 conversation 的 summary 字段
            conv = (await db.execute(
                select(Conversation).where(Conversation.id == conversation_id)
            )).scalar_one_or_none()
            if conv:
                old_summary = conv.summary or ""
                # 累积摘要（新旧融合）
                if old_summary:
                    conv.summary = await _merge_summaries(
                        old_summary, summary, model_type,
                    ) or summary
                else:
                    conv.summary = summary
                await db.commit()
            return summary
    except Exception as e:
        degraded("conv_summary", e)
    return None


async def _merge_summaries(old: str, new: str, model_type: str | None = None) -> str | None:
    """融合新旧两段摘要。"""
    provider = get_llm_provider(model_type)
    try:
        merged = await provider.chat(
            [{"role": "user", "content": (
                f"融合以下两段对话摘要为一段（100字内）：\n【旧摘要】{old}\n【新摘要】{new}"
            )}],
            temperature=0.2, max_tokens=200,
        )
        return (merged or "").strip()
    except Exception:
        return None

async def summarized_history_for_prompt(
    db: AsyncSession, conversation_id: str, history: list[dict],
) -> list[dict]:
    """P4-⑭ 接线：CONV_SUMMARY_ENABLE 开时返回「摘要 + 最近 K 条」，关=原样返回。

    - 历史未超阈值：原样返回（零开销）。
    - 超阈值且已有摘要：返回 [{user: 此前对话摘要}] + 最近 CONV_SUMMARY_KEEP_LAST 条。
    - 超阈值但无摘要：fire-and-forget 触发摘要（独立 session，防与请求 session 并发），
      本次仍返回全量（下次请求生效），不阻塞首答。
    """
    if not getattr(settings, "CONV_SUMMARY_ENABLE", False) or not conversation_id:
        return history
    threshold = int(getattr(settings, "CONV_SUMMARY_THRESHOLD", 8))
    keep = int(getattr(settings, "CONV_SUMMARY_KEEP_LAST", 4))
    if len(history) <= threshold:
        return history
    conv = (await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )).scalar_one_or_none()
    summary = ((conv.summary if conv else None) or "").strip()
    if not summary:
        async def _bg() -> None:
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as db2:
                await summarize_conversation(db2, conversation_id, history)
        try:
            import asyncio
            asyncio.create_task(_bg())
        except Exception as e:  # noqa: BLE001 — 摘要失败不影响本次问答
            degraded("conv_summary_kickoff", e)
        return history
    return ([{"role": "user",
              "content": f"[此前对话摘要，供上下文参考]\n{summary[:600]}"}]
            + history[-keep:])
