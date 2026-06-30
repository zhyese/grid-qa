"""混合检索编排：(可选query改写) 双collection并行 + BM25 + RRF + rerank + MMR + (可选docType过滤)。"""
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import milvus_client
from app.config import settings
from app.models.document import Document
from app.rag import mmr, rrf
from app.core.obs import degraded
from app.services import bm25_service, embedding_service, query_rewrite, rerank_service


def _to_item(h: dict) -> dict:
    return {
        "chunk": h.get("text", ""),
        "score": h.get("score", 0.0),
        "docId": h.get("doc_id", ""),
        "docName": h.get("doc_name", ""),
    }


async def mixed_search(
    db: AsyncSession, query: str, topk: int = 10,
    doc_type: str | None = None, model_type: str | None = None,
) -> list[dict]:
    import time
    _t0 = time.time()
    cand = max(topk * 4, 20)

    # 0) 可选 query 改写
    q = await query_rewrite.rewrite_query(query, model_type)

    # 1) 双 collection 并行 embedding + 并行查询
    qvec_cloud, qvec_bge = await asyncio.gather(
        embedding_service.embed_query(q, settings.EMB_PROVIDER),
        embedding_service.embed_query(q, "bge"),
    )
    dense_cloud, dense_bge = await asyncio.gather(
        asyncio.to_thread(milvus_client.search, settings.MILVUS_COLLECTION, qvec_cloud, cand),
        asyncio.to_thread(milvus_client.search, settings.MILVUS_COLLECTION_BGE, qvec_bge, cand),
    )
    dense = dense_cloud + dense_bge
    dense_hits = [{**d, "key": (d.get("doc_id"), d.get("chunk_idx"))} for d in dense]

    # 2) BM25 稀疏检索
    await bm25_service.ensure_built(db)
    sparse_hits = []
    for s in bm25_service.search(q, topk=cand):
        c = bm25_service.get_chunk(s["idx"])
        if not c:
            continue
        sparse_hits.append({
            "key": (c["doc_id"], c["chunk_idx"]),
            "text": c["text"], "doc_id": c["doc_id"],
            "doc_name": c["doc_name"], "chunk_idx": c["chunk_idx"],
        })

    # 3) RRF 融合
    fused = rrf.rrf_fuse([dense_hits, sparse_hits], key_fn=lambda h: h["key"])

    # 4) 重排（取 2*topk 候选供 MMR 选）
    if settings.RERANK_ENABLE and len(fused) > 1:
        try:
            docs = [h.get("text", "") for h in fused]
            ranked = await rerank_service.get_reranker().rerank(q, docs, top_n=min(topk * 2, len(fused)))
            pool = [{**fused[idx], "score": float(score)} for idx, score in ranked]
        except Exception as e:
            degraded("rerank", e)
            pool = fused[: topk * 2]
    else:
        pool = fused[: topk * 2]

    # 5) docType 元数据过滤
    if doc_type:
        doc_ids = {h.get("doc_id") for h in pool if h.get("doc_id")}
        rows = (await db.execute(
            select(Document.id, Document.doc_type).where(Document.id.in_(doc_ids))
        )).all()
        dt_map = {r[0]: r[1] for r in rows}
        pool = [h for h in pool if dt_map.get(h.get("doc_id")) == doc_type]

    # 6) MMR 多样性选 topk
    if settings.MMR_ENABLE and len(pool) > topk:
        pool = mmr.mmr(pool, topk, settings.MMR_LAMBDA)
    else:
        pool = pool[:topk]

    try:
        from app.core import metrics
        metrics.RETRIEVAL_LATENCY.observe(time.time() - _t0)
    except Exception:
        pass
    return [_to_item(h) for h in pool]
