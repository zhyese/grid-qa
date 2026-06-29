"""RAG 问答编排：检索 → 拼 prompt → LLM → 后处理（引用/安全提示/计时/幻觉率）。"""
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.factory import get_llm_provider
from app.rag import citation, prompt_templates
from app.services import retrieval_service, term_service


async def answer(
    db: AsyncSession, query: str, model_type: str | None = None, topk: int = 5
) -> dict:
    t0 = time.time()
    nq = term_service.normalize(query)  # 术语归一化提升检索召回

    contexts = await retrieval_service.mixed_search(db, nq, topk)
    if not contexts:
        return {
            "answer": "根据现有资料无法确认该问题，请先上传并解析相关运维文档后重试。",
            "retrievalSource": [],
            "responseTime": round(time.time() - t0, 3),
            "hallucinationRate": 0.0,
        }

    messages = prompt_templates.build_messages(
        nq, [{"docName": c["docName"], "chunk": c["chunk"]} for c in contexts]
    )
    llm = get_llm_provider(model_type)
    ans = await llm.chat(messages, temperature=0.2)

    return {
        "answer": ans,
        "retrievalSource": [c["chunk"][:200] for c in contexts],
        "responseTime": round(time.time() - t0, 3),
        "hallucinationRate": citation.estimate_hallucination(ans, len(contexts)),
    }
