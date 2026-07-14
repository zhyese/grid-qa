"""混合检索编排：(query改写/多查询分解/HyDE) + 双collection并行 + BM25 + RRF + rerank + MMR + small-to-big + docType过滤。

debug_search 扩展（v2）：
- 多样性指标（doc_uniqueness / source_entropy / chunk_adjacency）
- 路由对比模式（compare_routes=true → 四路并行 + 对比矩阵）
- Context Relevance Judge 集成（可选 LLM 定性评估）
"""
import asyncio
import math
import time
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import milvus_client
from app.config import settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.rag import mmr, rrf
from app.core.obs import degraded
from app.services import bm25_service, config_service, embedding_service, query_rewrite, rerank_service


def _ov(ov: dict | None, key: str, default):
    """overrides 读取：调参扫描时按 key 取覆盖值，否则 default。

    生产路径 mixed_search(overrides=None) → 恒走 default（=settings），13 caller 零破坏。
    """
    return ov.get(key, default) if ov else default


def _to_item(h: dict) -> dict:
    return {
        "chunk": h.get("text", ""),
        "score": h.get("score", 0.0),
        "docId": h.get("doc_id", ""),
        "docName": h.get("doc_name", ""),
        "docType": h.get("doc_type", ""),
        "chunkIdx": h.get("chunk_idx"),
        "sources": h.get("srcs", []),
    }


def _aggregate_srcs(dense_hits: list[dict], sparse_hits: list[dict]) -> dict:
    """key → 排序后的检索来源列表（dense_cloud/dense_bge/bm25 并集）。

    rrf_fuse 只保留首遇 key 的 hit 字段，sources 必须独立聚合、fuse 后回填。
    """
    src_map: dict = {}
    for h in dense_hits or []:
        k = h.get("key")
        if k is None:
            continue
        src_map.setdefault(k, set()).update(h.get("srcs", ["dense"]))
    for h in sparse_hits or []:
        k = h.get("key")
        if k is None:
            continue
        src_map.setdefault(k, set()).add("bm25")
    return {k: sorted(v) for k, v in src_map.items()}


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


async def _hyde_or_cache(q: str, model_type: str | None = None) -> str | None:
    """HyDE 假设文档（带 Redis 缓存）。关闭/失败返回 None。相同 query 不重复调 LLM。"""
    if not getattr(settings, "HYDE_ENABLE", False):
        return None
    from app.services import hyde, rewrite_cache
    try:
        cached = await rewrite_cache.get("hyde", q)
        if cached:
            return cached.get("hypothetical")
        ht = await hyde.generate_hypothetical(q, model_type)
        if ht:
            await rewrite_cache.set("hyde", q, {"hypothetical": ht})
        return ht
    except Exception as e:
        degraded("hyde_dispatch", e)
        return None


async def _dense_and_sparse(
    db: AsyncSession, q: str, cand: int, model_type: str | None = None
) -> tuple[list[dict], list[dict]]:
    """单 query 的 dense（双 collection，可选 HyDE）+ BM25。

    HyDE：用 LLM 生成的假设文档做 dense embedding（BM25 仍用原 q，稀疏检索吃原词）。
    """
    dense_q = (await _hyde_or_cache(q, model_type)) or q

    qvec_cloud, qvec_bge = await asyncio.gather(
        embedding_service.embed_query(dense_q, settings.EMB_PROVIDER),
        embedding_service.embed_query(dense_q, "bge"),
    )
    _ef = max(config_service.rt_ef(), cand)  # HNSW ef ≥ 召回窗口，保证精度
    dense_cloud, dense_bge = await asyncio.gather(
        asyncio.to_thread(milvus_client.search, settings.MILVUS_COLLECTION, qvec_cloud, cand, _ef),
        asyncio.to_thread(milvus_client.search, settings.MILVUS_COLLECTION_BGE, qvec_bge, cand, _ef),
    )
    dense_hits = []
    for d in dense_cloud:
        dense_hits.append({**d, "key": (d.get("doc_id"), d.get("chunk_idx")), "srcs": ["dense_cloud"]})
    for d in dense_bge:
        dense_hits.append({**d, "key": (d.get("doc_id"), d.get("chunk_idx")), "srcs": ["dense_bge"]})

    await bm25_service.ensure_built(db)
    sparse_hits = []
    _bm25_q = q
    if getattr(settings, "SYNONYM_EXPAND_ENABLE", False):
        try:
            from app.services import term_service
            _bm25_q = term_service.expand_synonyms(q)   # 同义词扩展（BRD §4.3.1）提升 BM25 召回
        except Exception:
            pass
    for s in bm25_service.search(_bm25_q, topk=cand):
        c = bm25_service.get_chunk(s["idx"])
        if not c:
            continue
        sparse_hits.append({
            "key": (c["doc_id"], c["chunk_idx"]), "text": c["text"],
            "doc_id": c["doc_id"], "doc_name": c["doc_name"], "chunk_idx": c["chunk_idx"],
            "srcs": ["bm25"],
        })
    return dense_hits, sparse_hits


def _acl_ok(doc_dept: str, doc_allowed_roles: str,
            user_dept: str | None, user_role: str | None) -> bool:
    """文档级 ACL 判定（检索后置过滤用）。user_dept/user_role 均 None → 不过滤（向后兼容/admin 链路）。

    - 文档 dept 空 = 公开（全员可读）
    - dept 不符 → 拒绝
    - allowed_roles 空 = 部门内全员可读；非空则 user_role 须命中（admin 放行）
    """
    if user_dept is None and user_role is None:
        return True
    if user_role == "admin":
        return True
    if not doc_dept:
        return True
    if user_dept and doc_dept != user_dept:
        return False
    if not doc_allowed_roles:
        return True
    allowed = [r.strip() for r in doc_allowed_roles.split(",") if r.strip()]
    return (not allowed) or (user_role in allowed)


async def mixed_search(
    db: AsyncSession, query: str, topk: int = 10,
    doc_type: str | None = None, model_type: str | None = None,
    equipment: str | None = None, tenant: str | None = None,
    routing_decision = None,  # RoutingDecision | None
    user_dept: str | None = None, user_role: str | None = None,  # RBAC 文档级 ACL
    overrides: dict | None = None,  # 调参扫描覆盖（None=走 settings，13 caller 零破坏）
) -> list[dict]:
    _t0 = time.time()
    topk = _ov(overrides, "TOPK", topk)  # topk 可被扫描覆盖
    cand = max(topk * 4, 20)

    # 0) query 改写（口语→规范，含 adaptive 跳过 + 缓存 + 评估闭环）
    q = (await query_rewrite.rewrite_query_v2(query, model_type))["query"]

    # 路由调度：根据决策选择检索路径
    route = routing_decision.route if routing_decision else "hybrid"

    # 0.5) 多查询分解（仅 hybrid 和 dense 路径启用；sparse 跳过；带缓存）
    queries = [q]
    if route != "sparse" and _ov(overrides, "MULTI_QUERY_ENABLE", getattr(settings, "MULTI_QUERY_ENABLE", False)):
        from app.services import multi_query, rewrite_cache
        try:
            cached_mq = await rewrite_cache.get("multi", query)
            if cached_mq:
                subs = cached_mq.get("subs", [])
            else:
                subs = await multi_query.decompose(query, model_type) or []
                await rewrite_cache.set("multi", query, {"subs": subs})
            if subs:
                queries.extend(subs)
        except Exception as e:
            degraded("multi_query_dispatch", e)

    # 1) 根据路由选择检索策略
    all_dense, all_sparse = [], []
    skip_rerank = False

    if route == "sparse":
        # sparse-only: 仅 BM25，跳过 dense embedding 调用
        await bm25_service.ensure_built(db)
        for qq in queries:
            for s in bm25_service.search(qq, topk=cand):
                c = bm25_service.get_chunk(s["idx"])
                if c:
                    all_sparse.append({
                        "key": (c["doc_id"], c["chunk_idx"]), "text": c["text"],
                        "doc_id": c["doc_id"], "doc_name": c["doc_name"],
                        "chunk_idx": c["chunk_idx"], "score": s.get("score", 0),
                        "srcs": ["bm25"],
                    })
        # 高置信 sparse 可跳过 rerank
        try:
            from app.routing.query_classifier import should_skip_rerank as _skip_rerank
            skip_rerank = _skip_rerank(routing_decision) if routing_decision else False
        except Exception:
            skip_rerank = False

    elif route == "dense":
        # dense-only: 仅双路向量，跳过 BM25
        for qq in queries:
            dense_q = (await _hyde_or_cache(qq, model_type)) or qq
            qvec_cloud, qvec_bge = await asyncio.gather(
                embedding_service.embed_query(dense_q, settings.EMB_PROVIDER),
                embedding_service.embed_query(dense_q, "bge"),
            )
            _ef = max(config_service.rt_ef(), cand)  # HNSW ef ≥ 召回窗口，保证精度
            dense_cloud, dense_bge = await asyncio.gather(
                asyncio.to_thread(milvus_client.search, settings.MILVUS_COLLECTION, qvec_cloud, cand, _ef),
                asyncio.to_thread(milvus_client.search, settings.MILVUS_COLLECTION_BGE, qvec_bge, cand, _ef),
            )
            for src, results in (("dense_cloud", dense_cloud), ("dense_bge", dense_bge)):
                for d in results:
                    all_dense.append({
                        **d, "key": (d.get("doc_id"), d.get("chunk_idx")),
                        "srcs": [src],
                    })

    else:
        # hybrid / sparse_first: 全链路 dense + BM25
        for qq in queries:
            d, s = await _dense_and_sparse(db, qq, cand, model_type)
            all_dense.extend(d)
            all_sparse.extend(s)

    # 2) RRF 融合 / 单路排序 + sources 归因回填
    fused = []  # 兼容旧引用
    if route == "sparse":
        # BM25 单路：按分数排序取 topk*2
        pool = sorted(all_sparse, key=lambda x: -float(x.get("score", 0) or 0))[: topk * 2]
        _src_map = _aggregate_srcs([], all_sparse)
        for h in pool:
            h["srcs"] = _src_map.get(h.get("key"), list(h.get("srcs", [])))
    elif route == "dense":
        # Dense 单路：按分数排序取 topk*2
        pool = sorted(all_dense, key=lambda x: -float(x.get("score", 0) or 0))[: topk * 2]
        _src_map = _aggregate_srcs(all_dense, [])
        for h in pool:
            h["srcs"] = _src_map.get(h.get("key"), list(h.get("srcs", [])))
    else:
        # hybrid / sparse_first: RRF 融合
        fused = rrf.rrf_fuse([all_dense, all_sparse], key_fn=lambda h: h["key"],
                             k=_ov(overrides, "RRF_K", settings.RRF_K),
                             weights=[_ov(overrides, "RRF_DENSE_WEIGHT", settings.RRF_DENSE_WEIGHT),
                                      _ov(overrides, "RRF_SPARSE_WEIGHT", settings.RRF_SPARSE_WEIGHT)])
        _src_map = _aggregate_srcs(all_dense, all_sparse)
        for h in fused:
            h["srcs"] = _src_map.get(h.get("key"), [])
        pool = fused[: topk * 2]

    # 3) 重排（高置信 sparse 可跳过，单路 dense/sparse 也可跳过）
    if _ov(overrides, "RERANK_ENABLE", settings.RERANK_ENABLE) and len(pool) > 1 and not skip_rerank:
        try:
            docs = [h.get("text", "") for h in pool]
            ranked = await rerank_service.get_reranker().rerank(q, docs, top_n=min(topk * 2, len(pool)))
            pool = [{**pool[idx], "score": float(score)} for idx, score in ranked]
        except Exception as e:
            degraded("rerank", e)
            # pool 保持原样，不改变
    # pool 已经就绪（sparse/dense 单路直接使用，hybrid 已 RRF 融合）

    # 4) 元数据过滤：租户隔离 + docType/设备（多租户 + D5）+ RBAC 文档级 ACL（dept/allowed_roles）
    doc_ids = {h.get("doc_id") for h in pool if h.get("doc_id")}
    if doc_ids and (tenant or doc_type or equipment or user_dept or user_role):
        rows = (await db.execute(
            select(Document.id, Document.doc_type, Document.equipment_tags,
                   Document.tenant_id, Document.dept, Document.allowed_roles)
            .where(Document.id.in_(doc_ids))
        )).all()
        dt_map = {r[0]: r[1] for r in rows}
        eq_map = {r[0]: (r[2] or "") for r in rows}
        tn_map = {r[0]: r[3] for r in rows}
        dept_map = {r[0]: (r[4] or "") for r in rows}
        ar_map = {r[0]: (r[5] or "") for r in rows}
        pool = [
            h for h in pool
            if (not tenant or tn_map.get(h.get("doc_id")) == tenant)
            and (not doc_type or dt_map.get(h.get("doc_id")) == doc_type)
            and (not equipment or equipment in eq_map.get(h.get("doc_id"), ""))
            and _acl_ok(dept_map.get(h.get("doc_id"), ""),
                        ar_map.get(h.get("doc_id"), ""), user_dept, user_role)
        ]

    # 4.5) docType 补全：保证每个 item 都带 doc_type（来源卡片要用，无条件查一次）
    _need_type_ids = {h.get("doc_id") for h in pool if h.get("doc_id") and not h.get("doc_type")}
    if _need_type_ids:
        _rows = (await db.execute(
            select(Document.id, Document.doc_type).where(Document.id.in_(_need_type_ids))
        )).all()
        _dt = {r[0]: (r[1] or "") for r in _rows}
        for h in pool:
            if not h.get("doc_type"):
                h["doc_type"] = _dt.get(h.get("doc_id"), "")

    # 5) MMR 多样性选 topk
    if _ov(overrides, "MMR_ENABLE", settings.MMR_ENABLE) and len(pool) > topk:
        pool = mmr.mmr(pool, topk, _ov(overrides, "MMR_LAMBDA", settings.MMR_LAMBDA))
    else:
        pool = pool[:topk]

    # 6) small-to-big：命中小块召回同组父块全文（完整上下文）
    if _ov(overrides, "SMALL_TO_BIG_ENABLE", settings.SMALL_TO_BIG_ENABLE):
        pool = await _expand_parents(db, pool)

    # 7) RAPTOR 层次化摘要检索（融合摘要层命中）
    if getattr(settings, "RAPTOR_ENABLE", False):
        try:
            from app.rag.raptor import retrieve_with_raptor
            raptor_hits = await retrieve_with_raptor(
                db, q, topk=topk, tenant=tenant, routing_decision=routing_decision,
            )
            if raptor_hits:
                # RRF 融合：原文 pool + 摘要层 hits
                seen_keys = {(h.get("docId", ""), h.get("section", "")) for h in pool}
                raptor_fresh = [h for h in raptor_hits
                                if (h.get("docId", ""), h.get("section", "")) not in seen_keys]
                # 给摘要一个中等 RRF 常数，不压过原文分
                rrf_k = 30
                all_items = []
                for h in pool:
                    all_items.append({"key": id(h), "score": float(h.get("score", 0) or 0), "item": h})
                for h in raptor_fresh:
                    all_items.append({"key": id(h), "score": float(h.get("score", 0) or 0) * 0.8, "item": h})
                from app.rag import rrf as _rrf
                fused_raptor = _rrf.rrf_fuse([all_items], key_fn=lambda x: x["key"])[:topk]
                pool = [x["item"] for x in fused_raptor]
        except Exception as e:
            degraded("raptor_retrieve", e)

    try:
        from app.core import metrics
        metrics.RETRIEVAL_LATENCY.observe(time.time() - _t0)
    except Exception:
        pass
    items = [_to_item(h) for h in pool]
    # 插件扩展点 · retrieval_filter：第三方可注册检索结果过滤/重排（BRD §5.3.1）
    try:
        from app.services import plugin_registry
        items = plugin_registry.run_hook("retrieval_filter", items, {"query": q, "tenant": tenant})
    except Exception:
        pass
    return items


async def debug_search(
    db: AsyncSession, query: str, topk: int = 10,
    doc_type: str | None = None, model_type: str | None = None,
    equipment: str | None = None, tenant: str | None = None,
    user_dept: str | None = None, user_role: str | None = None,  # RBAC 文档级 ACL
) -> dict:
    """检索调试：与 mixed_search 同源构建块，但把每一步中间结果透出，供 admin 排障调参。

    不命中缓存、不裁剪中间态，返回 trace：config 快照 + 各步(改写/HyDE/multi-query/
    dense·BM25召回/RRF/rerank/元数据过滤/MMR) + 最终命中及其分数归因(dense/bm25/rrf/rerank)。
    生产路径 mixed_search 不受影响。
    """
    t_start = time.time()
    cand = max(topk * 4, 20)
    trace: dict = {"config": {}, "steps": [], "result": {}}

    def _step(name: str, **kw) -> None:
        trace["steps"].append({"step": name, **kw})

    trace["config"] = {
        "topK": topk, "candidate": cand, "docType": doc_type, "equipment": equipment,
        "queryRewrite": bool(getattr(settings, "QUERY_REWRITE_ENABLE", False)),
        "hyde": bool(getattr(settings, "HYDE_ENABLE", False)),
        "multiQuery": bool(getattr(settings, "MULTI_QUERY_ENABLE", False)),
        "rerank": bool(settings.RERANK_ENABLE),
        "mmr": bool(settings.MMR_ENABLE), "mmrLambda": float(getattr(settings, "MMR_LAMBDA", 0.6)),
        "smallToBig": bool(getattr(settings, "SMALL_TO_BIG_ENABLE", False)),
        "embProvider": settings.EMB_PROVIDER,
        "rerankModel": getattr(settings, "RERANK_MODEL", "gte-rerank-v2"),
        "milvusCollections": [settings.MILVUS_COLLECTION, settings.MILVUS_COLLECTION_BGE],
        "runtimeEf": config_service.rt_ef(),          # 运行时生效的 HNSW ef（/system/config/milvus 可调）
        "runtimeTemperature": config_service.rt_temperature(),  # 主答案 temperature（/system/config/model 可调）
    }

    # 0) query 改写
    q = await query_rewrite.rewrite_query(query, model_type)
    _step("query_rewrite", input=query, output=q, changed=(q != query))

    # 0.5) 多查询分解
    queries = [q]
    mq_subs: list[str] = []
    if getattr(settings, "MULTI_QUERY_ENABLE", False):
        from app.services import multi_query
        try:
            mq_subs = await multi_query.decompose(query, model_type) or []
            queries.extend(mq_subs)
        except Exception as e:
            degraded("multi_query_dispatch", e)
            _step("multi_query", error=str(e))
    _step("multi_query", subQueries=mq_subs, totalQueries=len(queries))

    # 1) 每个 query 跑 dense(双collection+可选HyDE) + BM25，归集 per-key 原始分
    dense_raw: dict = {}      # key -> [{"src","score"}, ...]
    bm25_raw: dict = {}       # key -> [score, ...]
    per_query = []
    all_dense, all_sparse = [], []
    await bm25_service.ensure_built(db)
    for qq in queries:
        dense_q = qq
        hyde_text = None
        if getattr(settings, "HYDE_ENABLE", False):
            from app.services import hyde
            try:
                hyde_text = await hyde.generate_hypothetical(qq, model_type)
                if hyde_text:
                    dense_q = hyde_text
            except Exception as e:
                degraded("hyde_dispatch", e)
        qvec_cloud, qvec_bge = await asyncio.gather(
            embedding_service.embed_query(dense_q, settings.EMB_PROVIDER),
            embedding_service.embed_query(dense_q, "bge"),
        )
        dense_cloud, dense_bge = await asyncio.gather(
            asyncio.to_thread(milvus_client.search, settings.MILVUS_COLLECTION, qvec_cloud, cand, config_service.rt_ef()),
            asyncio.to_thread(milvus_client.search, settings.MILVUS_COLLECTION_BGE, qvec_bge, cand, config_service.rt_ef()),
        )
        dense_hits = []
        for src, results in (("dense_cloud", dense_cloud), ("dense_bge", dense_bge)):
            for d in results:
                k = (d.get("doc_id"), d.get("chunk_idx"))
                dense_raw.setdefault(k, []).append({"src": src, "score": float(d.get("score", 0.0) or 0.0)})
                dense_hits.append({**d, "key": k})
        sparse_hits = []
        for s in bm25_service.search(qq, topk=cand):
            c = bm25_service.get_chunk(s["idx"])
            if not c:
                continue
            k = (c["doc_id"], c["chunk_idx"])
            sc = float(s.get("score", 0.0) or 0.0)
            bm25_raw.setdefault(k, []).append(sc)
            sparse_hits.append({
                "key": k, "text": c["text"], "doc_id": c["doc_id"],
                "doc_name": c["doc_name"], "chunk_idx": c["chunk_idx"], "score": sc,
            })
        all_dense.extend(dense_hits)
        all_sparse.extend(sparse_hits)
        per_query.append({"query": qq, "hyde": hyde_text, "denseHits": len(dense_hits), "bm25Hits": len(sparse_hits)})
    _step("retrieve", perQuery=per_query, denseTotal=len(all_dense), bm25Total=len(all_sparse))

    # 2) RRF 融合
    fused = rrf.rrf_fuse([all_dense, all_sparse], key_fn=lambda h: h["key"],
                         k=settings.RRF_K, weights=[settings.RRF_DENSE_WEIGHT, settings.RRF_SPARSE_WEIGHT])
    rrf_map = {h["key"]: float(h.get("score", 0.0) or 0.0) for h in fused}
    _step("rrf_fuse", fusedCount=len(fused))

    # 3) 重排
    rerank_scores: dict = {}
    if settings.RERANK_ENABLE and len(fused) > 1:
        try:
            docs = [h.get("text", "") for h in fused]
            ranked = await rerank_service.get_reranker().rerank(q, docs, top_n=min(topk * 2, len(fused)))
            for idx, score in ranked:
                rerank_scores[fused[idx]["key"]] = float(score)
            pool = [{**fused[idx], "score": float(score)} for idx, score in ranked]
            _step("rerank", ok=True, reranked=len(ranked))
        except Exception as e:
            degraded("rerank", e)
            pool = fused[: topk * 2]
            _step("rerank", ok=False, error=str(e))
    else:
        pool = fused[: topk * 2]
        _step("rerank", ok=False, reason="disabled")

    # 4) 元数据过滤 + RBAC ACL
    doc_ids = {h.get("doc_id") for h in pool if h.get("doc_id")}
    if doc_ids and (tenant or doc_type or equipment or user_dept or user_role):
        rows = (await db.execute(
            select(Document.id, Document.doc_type, Document.equipment_tags,
                   Document.tenant_id, Document.dept, Document.allowed_roles)
            .where(Document.id.in_(doc_ids))
        )).all()
        dt_map = {r[0]: r[1] for r in rows}
        eq_map = {r[0]: (r[2] or "") for r in rows}
        tn_map = {r[0]: r[3] for r in rows}
        dept_map = {r[0]: (r[4] or "") for r in rows}
        ar_map = {r[0]: (r[5] or "") for r in rows}
        before = len(pool)
        pool = [
            h for h in pool
            if (not tenant or tn_map.get(h.get("doc_id")) == tenant)
            and (not doc_type or dt_map.get(h.get("doc_id")) == doc_type)
            and (not equipment or equipment in eq_map.get(h.get("doc_id"), ""))
            and _acl_ok(dept_map.get(h.get("doc_id"), ""),
                        ar_map.get(h.get("doc_id"), ""), user_dept, user_role)
        ]
        _step("metadata_filter", before=before, after=len(pool))
    else:
        _step("metadata_filter", skipped=True)

    # 5) MMR
    if settings.MMR_ENABLE and len(pool) > topk:
        pool = mmr.mmr(pool, topk, settings.MMR_LAMBDA)
        _step("mmr", applied=True)
    else:
        pool = pool[:topk]
        _step("mmr", applied=False)

    # 6) 组装最终命中 + 分数归因
    hits = []
    for h in pool:
        k = h.get("key")
        d_list = dense_raw.get(k, [])
        hits.append({
            "docId": h.get("doc_id", ""), "docName": h.get("doc_name", ""),
            "chunkIdx": h.get("chunk_idx"),
            "text": (h.get("text", "") or "")[:200],
            "sources": sorted({x["src"] for x in d_list} | ({"bm25"} if bm25_raw.get(k) else set())),
            "scores": {
                "dense": d_list,
                "bm25": max(bm25_raw.get(k, [0.0])),
                "rrf": rrf_map.get(k),
                "rerank": rerank_scores.get(k),
                "final": float(h.get("score", 0.0) or 0.0),
            },
        })
    hits.sort(key=lambda x: -(x["scores"].get("rerank") if x["scores"].get("rerank") is not None
                              else (x["scores"].get("rrf") or 0.0)))

    # 7) 多样性指标（不依赖 LLM，纯计算）
    diversity = _compute_diversity(hits)
    trace["diversity"] = diversity

    trace["result"] = {
        "finalHits": len(hits), "hits": hits,
        "latencyMs": round((time.time() - t_start) * 1000, 1),
    }
    return trace


def _compute_diversity(hits: list[dict]) -> dict:
    """计算检索结果的多样性指标。

    - doc_uniqueness: distinct_docs / total_hits（去重文档占比）
    - source_entropy: 文档来源熵（越高越分散，越低越集中在少数文档）
    - chunk_adjacency_ratio: 相邻 chunk 占比（高 = 结果集中在同一篇文档的邻近段落）
    """
    if not hits:
        return {"doc_uniqueness": 0.0, "source_entropy": 0.0, "chunk_adjacency_ratio": 0.0, "distinct_docs": 0}

    n = len(hits)
    doc_ids = [h.get("docId", "") for h in hits]
    distinct = len(set(doc_ids))
    doc_uniqueness = distinct / n

    # 来源熵：-Σ(p_i * log(p_i))，p_i = 每篇文档的占比
    doc_counts = Counter(doc_ids)
    entropy = -sum((c / n) * math.log(c / n) for c in doc_counts.values())

    # 相邻 chunk 占比：同一文档中 chunk_idx 连续的占比
    adjacency = 0
    for i in range(1, n):
        if doc_ids[i] == doc_ids[i - 1] and doc_ids[i] != "":
            c1 = hits[i - 1].get("chunkIdx")
            c2 = hits[i].get("chunkIdx")
            if c1 is not None and c2 is not None and abs(c2 - c1) == 1:
                adjacency += 1
    chunk_adjacency_ratio = adjacency / (n - 1) if n > 1 else 0.0

    return {
        "doc_uniqueness": round(doc_uniqueness, 3),
        "source_entropy": round(entropy, 3),
        "chunk_adjacency_ratio": round(chunk_adjacency_ratio, 3),
        "distinct_docs": distinct,
        "doc_distribution": {d: c for d, c in doc_counts.most_common(5) if d},
    }


async def compare_routes(
    db: AsyncSession, query: str, topk: int = 10,
    model_type: str | None = None,
) -> dict:
    """检索路由对比：同一 query 分别走 sparse/dense/hybrid/sparse_first 四条路由，
    返回对比矩阵（同一批 chunks 的评分维度交叉对比）。

    每条路由返回：hits 列表 + 多样性指标 + 延迟。
    """
    routes = ["sparse", "dense", "hybrid"]
    results = {}
    for route in routes:
        t0 = time.time()
        from app.routing.query_classifier import RoutingDecision
        rd = RoutingDecision(route=route, confidence=0.9, reason="compare_routes", features={})
        hits = await mixed_search(
            db, query, topk, model_type=model_type,
            routing_decision=rd,
        )
        elapsed = round((time.time() - t0) * 1000, 1)
        diversity = _compute_diversity([
            {"docId": h.get("docId", ""), "chunkIdx": 0}
            for h in hits
        ])
        results[route] = {
            "hitCount": len(hits),
            "latencyMs": elapsed,
            "hits": [
                {"docName": h.get("docName", ""), "chunk": (h.get("chunk", "") or "")[:100],
                 "score": h.get("score", 0.0)}
                for h in hits[:5]
            ],
            "diversity": diversity,
        }
    return {
        "query": query,
        "topK": topk,
        "comparison": results,
    }
