"""混合检索编排：Milvus 稠密检索 + BM25 稀疏检索 + RRF 融合。"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import milvus_client
from app.rag import rrf
from app.services import bm25_service, embedding_service


async def mixed_search(db: AsyncSession, query: str, topk: int = 10) -> list[dict]:
    # 1) 向量稠密检索（Milvus）
    qvec = await embedding_service.embed_query(query)
    dense = milvus_client.search(qvec, topk=max(topk * 2, 20))
    dense_hits = [
        {**d, "key": (d.get("doc_id"), d.get("chunk_idx"))} for d in dense
    ]

    # 2) BM25 稀疏检索（内存语料）
    await bm25_service.ensure_built(db)
    sparse_hits = []
    for s in bm25_service.search(query, topk=max(topk * 2, 20)):
        c = bm25_service.get_chunk(s["idx"])
        if not c:
            continue
        sparse_hits.append(
            {
                "key": (c["doc_id"], c["chunk_idx"]),
                "text": c["text"], "doc_id": c["doc_id"],
                "doc_name": c["doc_name"], "chunk_idx": c["chunk_idx"],
            }
        )

    # 3) RRF 融合
    fused = rrf.rrf_fuse([dense_hits, sparse_hits], key_fn=lambda h: h["key"])
    return [
        {
            "chunk": h.get("text", ""), "score": h["score"],
            "docId": h.get("doc_id", ""), "docName": h.get("doc_name", ""),
        }
        for h in fused[:topk]
    ]
