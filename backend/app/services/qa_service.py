"""RAG 问答编排：热点缓存 → 检索 → prompt → LLM → 后处理。"""
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import redis_client
from app.config import settings
from app.providers.factory import get_llm_provider
from app.rag import citation, prompt_templates
from app.services import retrieval_service, term_service


def _cache_key(model_type: str | None, query: str) -> str:
    return f"qa:{model_type or 'default'}:{query}"


async def answer(
    db: AsyncSession, query: str, model_type: str | None = None, topk: int = 5
) -> dict:
    t0 = time.time()
    nq = term_service.normalize(query)

    # 1) 热点缓存命中 → 直接返回（省检索+LLM）
    key = _cache_key(model_type, nq)
    try:
        cached = await redis_client.cache_get_json(key)
    except Exception:
        cached = None
    if cached:
        cached["cached"] = True
        cached["responseTime"] = round(time.time() - t0, 3)
        return cached

    # 2) 混合检索
    contexts = await retrieval_service.mixed_search(db, nq, topk)
    if not contexts:
        return {
            "answer": "根据现有资料无法确认该问题，请先上传并解析相关运维文档后重试。",
            "retrievalSource": [],
            "responseTime": round(time.time() - t0, 3),
            "hallucinationRate": 0.0,
            "cached": False,
        }

    # 3) 拼 prompt → LLM
    messages = prompt_templates.build_messages(
        nq, [{"docName": c["docName"], "chunk": c["chunk"]} for c in contexts]
    )
    ans = await get_llm_provider(model_type).chat(messages, temperature=0.2)

    result = {
        "answer": ans,
        "retrievalSource": [c["chunk"][:200] for c in contexts],
        "responseTime": round(time.time() - t0, 3),
        "hallucinationRate": citation.estimate_hallucination(ans, len(contexts)),
        "cached": False,
    }

    # 4) 写入热点缓存（高频问题下次秒回）
    try:
        await redis_client.cache_set_json(key, result, settings.QA_CACHE_TTL)
    except Exception:
        pass
    return result


async def stream_answer(
    db: AsyncSession, query: str, model_type: str | None = None, topk: int = 5
):
    """流式问答：检索 → LLM 逐 token yield（不走缓存，首字 <1s）。"""
    nq = term_service.normalize(query)
    contexts = await retrieval_service.mixed_search(db, nq, topk)
    if not contexts:
        yield "根据现有资料无法确认该问题，请先上传并解析相关运维文档后重试。"
        return
    messages = prompt_templates.build_messages(
        nq, [{"docName": c["docName"], "chunk": c["chunk"]} for c in contexts]
    )
    async for token in get_llm_provider(model_type).stream(messages, temperature=0.2):
        yield token
