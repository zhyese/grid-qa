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
    ans = await get_llm_provider(model_type).chat(messages, temperature=0.2)

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
    return result


async def stream_answer(
    db: AsyncSession, query: str, model_type: str | None = None,
    topk: int = 5, conversation_id: str | None = None,
):
    """流式问答：检索 → LLM 逐 token yield。"""
    nq = term_service.normalize(query)
    contexts = await retrieval_service.mixed_search(db, nq, topk)
    if not contexts:
        yield "根据现有资料无法确认该问题，请先上传并解析相关运维文档后重试。"
        return
    history = []
    if conversation_id:
        history = await conversation_service.get_messages(db, conversation_id, _HISTORY_LIMIT)
    messages = prompt_templates.build_messages_with_history(nq, contexts, history)
    async for token in get_llm_provider(model_type).stream(messages, temperature=0.2):
        yield token
