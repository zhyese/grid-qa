"""混合检索编排：(query改写/多查询分解/HyDE) + 双collection并行 + BM25 + RRF + rerank + MMR + small-to-big + docType过滤。"""
import asyncio
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import milvus_client
from app.config import settings
from app.models.chunk import Chunk
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


async def _expand_parents(db: AsyncSession, pool: list[dict]) -> list[dict]:
    """small-to-big：命中小块 → 聚合同组父块全文给 LLM（完整上下文，解决跨块/表格被切）。

    关闭(SMALL_TO_BIG_ENABLE=False)或查不到 parent 时原样返回（兼容旧行为）。
    """
    if not pool or not getattr(settings, "SMALL_TO_BIG_ENABLE", False):
        return pool
    doc_ids = list({h.get("doc_id") for h in pool if h.get("doc_id")})
    if not doc_ids:
        return pool
    rows = (await db.execute(
        select(Chunk.doc_id, Chunk.chunk_idx, Chunk.parent_idx, Chunk.content)
        .where(Chunk.doc_id.in_(doc_ids))
    )).all()
    chunk_to_parent: dict = {}
    group_content: dict = {}
    for doc_id, cidx, pidx, content in rows:
        chunk_to_parent[(doc_id, cidx)] = pidx
        group_content.setdefault((doc_id, pidx), []).append((cidx, content or ""))
    out, used = [], set()
    for h in sorted(pool, key=lambda x: -float(x.get("score", 0) or 0)):
        doc_id, cidx = h.get("doc_id"), h.get("chunk_idx")
        pidx = chunk_to_parent.get((doc_id, cidx))
        if pidx is None or (doc_id, pidx) in used:
            continue
        used.add((doc_id, pidx))
        members = sorted(group_content.get((doc_id, pidx), []), key=lambda x: x[0])
        parent_text = "\n".join(c for _, c in members if c) or h.get("text", "")
        out.append({**h, "text": parent_text})
    return out or pool


async def _dense_and_sparse(
    db: AsyncSession, q: str, cand: int, model_type: str | None = None
) -> tuple[list[dict], list[dict]]:
    """单 query 的 dense（双 collection，可选 HyDE）+ BM25。

    HyDE：用 LLM 生成的假设文档做 dense embedding（BM25 仍用原 q，稀疏检索吃原词）。
    """
    dense_q = q
    if getattr(settings, "HYDE_ENABLE", False):
        from app.services import hyde
        try:
            ht = await hyde.generate_hypothetical(q, model_type)
            if ht:
                dense_q = ht
        except Exception as e:
            degraded("hyde_dispatch", e)

    qvec_cloud, qvec_bge = await asyncio.gather(
        embedding_service.embed_query(dense_q, settings.EMB_PROVIDER),
        embedding_service.embed_query(dense_q, "bge"),
    )
    dense_cloud, dense_bge = await asyncio.gather(
        asyncio.to_thread(milvus_client.search, settings.MILVUS_COLLECTION, qvec_cloud, cand),
        asyncio.to_thread(milvus_client.search, settings.MILVUS_COLLECTION_BGE, qvec_bge, cand),
    )
    dense_hits = [
        {**d, "key": (d.get("doc_id"), d.get("chunk_idx"))} for d in (dense_cloud + dense_bge)
    ]

    await bm25_service.ensure_built(db)
    sparse_hits = []
    for s in bm25_service.search(q, topk=cand):
        c = bm25_service.get_chunk(s["idx"])
        if not c:
            continue
        sparse_hits.append({
            "key": (c["doc_id"], c["chunk_idx"]), "text": c["text"],
            "doc_id": c["doc_id"], "doc_name": c["doc_name"], "chunk_idx": c["chunk_idx"],
        })
    return dense_hits, sparse_hits


async def mixed_search(
    db: AsyncSession, query: str, topk: int = 10,
    doc_type: str | None = None, model_type: str | None = None,
    equipment: str | None = None,
) -> list[dict]:
    _t0 = time.time()
    cand = max(topk * 4, 20)

    # 0) query 改写（口语→规范）
    q = await query_rewrite.rewrite_query(query, model_type)

    # 0.5) 多查询分解：复杂问题拆子问题，每个独立检索后候选合并（默认关）
    queries = [q]
    if getattr(settings, "MULTI_QUERY_ENABLE", False):
        from app.services import multi_query
        try:
            subs = await multi_query.decompose(query, model_type)
            if subs:
                queries.extend(subs)
        except Exception as e:
            degraded("multi_query_dispatch", e)

    # 1) 对每个 query 跑 dense + BM25，合并候选（跨查询同 chunk 多次命中→RRF 累加）
    all_dense, all_sparse = [], []
    for qq in queries:
        d, s = await _dense_and_sparse(db, qq, cand, model_type)
        all_dense.extend(d)
        all_sparse.extend(s)

    # 2) RRF 融合
    fused = rrf.rrf_fuse([all_dense, all_sparse], key_fn=lambda h: h["key"])

    # 3) 重排（取 2*topk 候选供 MMR 选）
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

    # 4) docType / equipment 元数据过滤（设备台账关联 D5）
    if doc_type or equipment:
        doc_ids = {h.get("doc_id") for h in pool if h.get("doc_id")}
        rows = (await db.execute(
            select(Document.id, Document.doc_type, Document.equipment_tags)
            .where(Document.id.in_(doc_ids))
        )).all()
        dt_map = {r[0]: r[1] for r in rows}
        eq_map = {r[0]: (r[2] or "") for r in rows}
        pool = [
            h for h in pool
            if (not doc_type or dt_map.get(h.get("doc_id")) == doc_type)
            and (not equipment or equipment in eq_map.get(h.get("doc_id"), ""))
        ]

    # 5) MMR 多样性选 topk
    if settings.MMR_ENABLE and len(pool) > topk:
        pool = mmr.mmr(pool, topk, settings.MMR_LAMBDA)
    else:
        pool = pool[:topk]

    # 6) small-to-big：命中小块召回同组父块全文（完整上下文）
    if settings.SMALL_TO_BIG_ENABLE:
        pool = await _expand_parents(db, pool)

    try:
        from app.core import metrics
        metrics.RETRIEVAL_LATENCY.observe(time.time() - _t0)
    except Exception:
        pass
    return [_to_item(h) for h in pool]
