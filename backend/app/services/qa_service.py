"""RAG 问答编排：热点缓存 / 多轮上下文 / 检索 / prompt / LLM / 后处理。"""
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import redis_client
from app.config import settings
from app.providers.factory import get_llm_provider
from app.rag import citation, prompt_templates
from app.services import conversation_service, retrieval_service, term_service

_HISTORY_LIMIT = 6  # 拼接最近 3 轮（6 条消息）


def _cache_key(model_type: str | None, query: str) -> str:
    return f"qa:{model_type or 'default'}:{query}"


async def answer(
    db: AsyncSession, query: str, model_type: str | None = None,
    topk: int = 5, conversation_id: str | None = None, username: str = "",
) -> dict:
    t0 = time.time()
    nq = term_service.normalize(query)

    # 多轮不走缓存（上下文变化）；单轮命中热点缓存
    if not conversation_id:
        try:
            cached = await redis_client.cache_get_json(_cache_key(model_type, nq))
        except Exception:
            cached = None
        if cached:
            cached["cached"] = True
            cached["responseTime"] = round(time.time() - t0, 3)
            try:
                from app.core import metrics
                metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "true").inc()
            except Exception:
                pass
            return cached

    contexts = await retrieval_service.mixed_search(db, nq, topk)
    if not contexts:
        return {
            "answer": "根据现有资料无法确认该问题，请先上传并解析相关运维文档后重试。",
            "retrievalSource": [], "responseTime": round(time.time() - t0, 3),
            "hallucinationRate": 0.0, "cached": False, "conversationId": conversation_id or "",
        }

    # 多轮历史
    history = []
    if conversation_id:
        history = await conversation_service.get_messages(db, conversation_id, _HISTORY_LIMIT)
    messages = prompt_templates.build_messages_with_history(nq, contexts, history)
    _llm0 = time.time()
    ans = await get_llm_provider(model_type).chat(messages, temperature=0.2)
    try:
        from app.core import metrics
        _p = model_type or settings.LLM_PROVIDER
        metrics.LLM_CALLS.labels(_p).inc()
        metrics.LLM_LATENCY.labels(_p).observe(time.time() - _llm0)
    except Exception:
        pass

    # 持久化对话
    if not conversation_id:
        conv = await conversation_service.create_conversation(db, username, query)
        conversation_id = conv.id
    await conversation_service.save_message(db, conversation_id, "user", query)
    await conversation_service.save_message(db, conversation_id, "assistant", ans)

    result = {
        "answer": ans,
        "retrievalSource": [c["chunk"][:200] for c in contexts],
        "responseTime": round(time.time() - t0, 3),
        "hallucinationRate": citation.estimate_hallucination(ans, len(contexts)),
        "cached": False,
        "conversationId": conversation_id,
    }

    # 仅单轮结果写缓存
    try:
        await redis_client.cache_set_json(_cache_key(model_type, nq), result, settings.QA_CACHE_TTL)
    except Exception:
        pass
    try:
        from app.core import metrics
        metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "false").inc()
    except Exception:
        pass
    return result


async def stream_answer(
    db: AsyncSession, query: str, model_type: str | None = None,
    topk: int = 5, conversation_id: str | None = None, username: str = "",
):
    """流式问答：meta(引用+会话) → 逐 token → done(耗时+幻觉率)。

    若只流 token 会丢失引用溯源/耗时/反馈所需元数据，故分三段事件下发。
    """
    t0 = time.time()
    nq = term_service.normalize(query)
    contexts = await retrieval_service.mixed_search(db, nq, topk)
    if not contexts:
        yield {"type": "done", "content": "根据现有资料无法确认该问题，请先上传并解析相关运维文档后重试。"}
        return

    # 多轮历史
    history = []
    if conversation_id:
        history = await conversation_service.get_messages(db, conversation_id, _HISTORY_LIMIT)
    messages = prompt_templates.build_messages_with_history(nq, contexts, history)

    # 流式前先建会话，确保 conversationId 可随 meta 下发
    if not conversation_id:
        conv = await conversation_service.create_conversation(db, username, query)
        conversation_id = conv.id

    # 1) meta：引用来源 + 会话 ID（前端据此显示溯源、刷新侧栏）
    yield {
        "type": "meta",
        "sources": [c["chunk"][:200] for c in contexts],
        "conversationId": conversation_id,
    }

    # 2) 逐 token 流式（打字机效果）
    parts: list[str] = []
    async for token in get_llm_provider(model_type).stream(messages, temperature=0.2):
        parts.append(token)
        yield {"type": "token", "content": token}

    # 3) 持久化完整答案 + done（耗时/幻觉率）
    full = "".join(parts)
    try:
        await conversation_service.save_message(db, conversation_id, "user", query)
        await conversation_service.save_message(db, conversation_id, "assistant", full)
    except Exception:
        pass
    try:
        from app.core import metrics
        metrics.QA_TOTAL.labels(model_type or settings.LLM_PROVIDER, "false").inc()
    except Exception:
        pass
    yield {
        "type": "done",
        "responseTime": round(time.time() - t0, 3),
        "hallucinationRate": citation.estimate_hallucination(full, len(contexts)),
        "conversationId": conversation_id,
    }
