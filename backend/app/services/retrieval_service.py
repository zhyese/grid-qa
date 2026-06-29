"""混合检索编排：双 collection(云+bge) 稠密 + BM25 稀疏 + RRF 融合 + 重排。"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import milvus_client
from app.config import settings
from app.rag import rrf
from app.services import bm25_service, embedding_service, rerank_service


def _to_item(h: dict) -> dict:
    return {
        "chunk": h.get("text", ""),
        "score": h.get("score", 0.0),
        "docId": h.get("doc_id", ""),
        "docName": h.get("doc_name", ""),
    }


async def mixed_search(db: AsyncSession, query: str, topk: int = 10) -> list[dict]:
    cand = max(topk * 4, 20)

    # 1) 双 collection 稠密检索（云 + 本地 bge，并行 embedding + 并行查询）
    import asyncio

    qvec_cloud, qvec_bge = await asyncio.gather(
        embedding_service.embed_query(query, settings.EMB_PROVIDER),
        embedding_service.embed_query(query, "bge"),
    )
    dense_cloud, dense_bge = await asyncio.gather(
        asyncio.to_thread(milvus_client.search, settings.MILVUS_COLLECTION, qvec_cloud, cand),
        asyncio.to_thread(milvus_client.search, settings.MILVUS_COLLECTION_BGE, qvec_bge, cand),
    )
    dense = dense_cloud + dense_bge
    dense_hits = [{**d, "key": (d.get("doc_id"), d.get("chunk_idx"))} for d in dense]

    # 2) BM25 稀疏检索（内存语料，覆盖两路 chunk 文本）
    await bm25_service.ensure_built(db)
    sparse_hits = []
    for s in bm25_service.search(query, topk=cand):
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

    # 4) 重排（百炼 gte-rerank），失败兜底 RRF
    if settings.RERANK_ENABLE and len(fused) > 1:
        try:
            docs = [h.get("text", "") for h in fused]
            ranked = await rerank_service.get_reranker().rerank(query, docs, top_n=topk)
            out = []
            for idx, score in ranked:
                out.append({**_to_item(fused[idx]), "score": round(float(score), 4)})
            if out:
                return out
        except Exception:
            pass

    return [_to_item(h) for h in fused[:topk]]
