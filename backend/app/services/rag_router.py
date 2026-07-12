"""双 RAG 框架热备（BRD §5.2.3）。

主路 A：qa_service.answer（Milvus dense + rerank + CRAG + 三级缓存 + GraphRAG）。
副路 B：BM25 + 直接 LLM（jieba 倒排，只依赖 MySQL；不碰 Milvus / embedding / rerank）。

两条路真正独立：A 的关键依赖是 Milvus(dense 向量检索)，B 完全不用向量库。
A 抛异常（如 Milvus 不可用 / embed 失败）→ 自动热切换到 B，失败可见（degraded rag_failover）。
"""
import asyncio
import time

from app.core.obs import degraded
from app.providers.factory import get_llm_provider
from app.rag.prompt_templates import build_messages
from app.services import bm25_service, qa_service
from app.services.config_service import rt_temperature


async def primary_health() -> dict:
    """主路关键依赖探活：Milvus（dense 检索基石）。返回 {milvus, primary}。"""
    milvus_ok = False
    detail = ""
    try:
        from app.clients import milvus_client
        n = await asyncio.to_thread(milvus_client.num_entities)
        milvus_ok = n >= 0  # 能取到 num_entities 即连通
        detail = f"num_entities={n}"
    except Exception as e:
        degraded("rag_primary_health", e)
        detail = f"{type(e).__name__}"
    return {"milvus": "ok" if milvus_ok else "down", "detail": detail,
            "primary": "available" if milvus_ok else "unavailable"}


async def answer_redundant(db, query: str, model_type: str | None, **kw) -> dict:
    """主路优先，异常自动切副路 B。kw 透传给 qa_service.answer。"""
    try:
        data = await qa_service.answer(db, query, model_type, **kw)
        data.setdefault("framework", "primary")
        data["failover"] = False
        return data
    except Exception as e:
        degraded("rag_failover_to_secondary", e)
        return await _secondary_bm25_llm(db, query, model_type)


async def _secondary_bm25_llm(db, query: str, model_type: str | None) -> dict:
    """副路 B：BM25 检索 + 直接 LLM（独立于 Milvus / embedding / rerank）。"""
    t0 = time.time()
    await bm25_service.ensure_built(db)
    hits = bm25_service.search(query, topk=8) or []
    contexts = []
    for h in hits:
        c = bm25_service.get_chunk(h.get("idx"))
        if c:
            contexts.append({"docName": c.get("docName") or c.get("doc_name") or "",
                             "chunk": c.get("content") or c.get("text") or ""})
    messages = build_messages(query, contexts)
    ans = await get_llm_provider(model_type).chat(messages, temperature=rt_temperature())
    return {
        "answer": ans,
        "retrievalSource": [{"docName": c["docName"], "text": c["chunk"][:200]} for c in contexts],
        "framework": "secondary_bm25_llm",
        "failover": True,
        "cached": False,
        "responseTime": round(time.time() - t0, 3),
    }
