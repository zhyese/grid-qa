"""知识库自进化编排器：dislike 聚类 → 盲区 → LLM 草稿 → 审核回流。

复刻 knowledge_governance 的 scan 入队 + review 审核双范式。
生长顺序：T3 聚类 → T4 抽取/盲区 → T5 草稿 → T6 编排 → T8 审核 → T9 回流。
"""
import asyncio
import json
import math
import time
import uuid
from datetime import timedelta

from sqlalchemy import delete, func, select

from app.core.obs import degraded
from app.models.evidence_gap import EvidenceGap
from app.models.feedback import Feedback
from app.models.knowledge_evolution import KnowledgeEvolutionDraft
from app.services.task_queue_service import utcnow

# ===== 常量（spec global constraints）=====
TASK_TYPE = "knowledge_evolution.scan"
CLUSTER_THRESHOLD = 0.82
CLUSTER_MIN_SIZE = 2            # 原3; 降到2让少量燃料也能成簇产出草稿
BLIND_TOP1_THRESHOLD = 0.55
AI_QUALITY_SCORE = 0.6
WEEKLY_QUOTA_DEFAULT = 20


# ===== T3: 零依赖贪心近邻聚类 =====
def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _mean_vec(members):
    n = len(members)
    dim = len(members[0]["vec"])
    return [sum(m["vec"][i] for m in members) / n for i in range(dim)]


def cluster(items, threshold=CLUSTER_THRESHOLD, min_size=CLUSTER_MIN_SIZE):
    """零依赖贪心近邻聚类。items=[{query, vec}]；返回 [{cluster_id, representative_query, members, centroid}]。"""
    clusters = []
    for it in items:
        placed = False
        for c in clusters:
            if _cosine(it["vec"], c["centroid"]) >= threshold:
                c["members"].append(it)
                c["centroid"] = _mean_vec(c["members"])
                placed = True
                break
        if not placed:
            clusters.append({"cluster_id": uuid.uuid4().hex[:12], "centroid": list(it["vec"]), "members": [it]})
    out = []
    for c in clusters:
        if len(c["members"]) < min_size:
            continue
        rep = max(c["members"], key=lambda m: _cosine(m["vec"], c["centroid"]))
        c["representative_query"] = rep["query"]
        out.append(c)
    return out


# ===== T4: 抽取 dislike + 盲区判定 =====
async def _retrieve_top1(db, query, tenant, top_k=1):
    from app.services import retrieval_service
    items = await retrieval_service.mixed_search(db, query, topk=top_k, tenant=tenant)
    return [{
        "score": float(it.get("score", 0.0) or 0.0),
        "doc_id": it.get("docId", "") or it.get("doc_id", ""),
    } for it in items]


async def _extract_dislike(db, tenant, since_hours):
    cutoff = utcnow() - timedelta(hours=since_hours)
    stmt = select(Feedback.query).where(Feedback.feedback == "dislike", Feedback.created_at >= cutoff)
    if hasattr(Feedback, "tenant_id"):
        stmt = stmt.where(Feedback.tenant_id == tenant)
    rows = (await db.execute(stmt)).scalars().all()
    queries = [q.strip() for q in rows if q and q.strip()]
    seen = set(queries)
    # EvidenceGap 租户字段为 tenant（非 tenant_id）；is_deleted 逻辑删除保护
    gaps = (await db.execute(
        select(EvidenceGap.query).where(
            EvidenceGap.status == "pending",
            EvidenceGap.is_deleted == 0,
            EvidenceGap.tenant == tenant,
        )
    )).scalars().all()
    for q in gaps:
        q = (q or "").strip()
        if q and q not in seen:
            seen.add(q)
            queries.append(q)
    return queries


async def _identify_blind_spot(db, cluster_obj, tenant):
    top = await _retrieve_top1(db, cluster_obj["representative_query"], tenant, top_k=1)
    if not top:
        return {"top1_score": 0.0, "hit_doc_ids": [], "confidence": "blind"}
    score = top[0]["score"]
    if score >= BLIND_TOP1_THRESHOLD:
        return None
    return {"top1_score": score, "hit_doc_ids": [top[0]["doc_id"]], "confidence": "medium"}


# ===== T5: 草稿生成（LLM + RAG 增强，防胡编）=====
PROMPT_TMPL = """你是电网运维知识工程师。基于高频用户疑问（系统未能很好回答）和参考资料，编写一条结构化知识条目。
必须严格基于参考资料，不得编造，标注来源。只输出 JSON。
【用户疑问簇】{queries}
【参考资料】{docs}
输出 JSON: {{"title":"简洁标题","content":"现象/原因/处置/依据的结构化正文","source_refs":["doc_id"]}}"""


async def _recent_standards(db, query, tenant, top_k=3):
    """最近规程文档：Document recency desc。返回 [{doc_id, name, snippet}]。"""
    from app.models.document import Document
    rows = (await db.execute(
        select(Document).where(Document.tenant_id == tenant).order_by(Document.created_at.desc()).limit(top_k * 5)
    )).scalars().all()
    return [{"doc_id": str(r.id), "name": r.doc_name, "snippet": (r.doc_name or "")[:120]} for r in rows[:top_k]]


async def _call_llm_json(prompt, model_type):
    from app.providers.factory import get_llm_provider
    return await get_llm_provider(model_type).chat(
        [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=800)


async def _generate_draft(db, cluster_obj, evidence, tenant, model_type):
    queries = [m["query"] for m in cluster_obj["members"]]
    docs = await _recent_standards(db, cluster_obj["representative_query"], tenant)
    prompt = PROMPT_TMPL.format(queries=queries[:10], docs=docs)
    raw = await _call_llm_json(prompt, model_type)
    try:
        obj = json.loads(raw)
    except Exception:
        obj = {"title": cluster_obj["representative_query"][:64], "content": (raw or "")[:2000], "source_refs": []}
    return {
        "draft_title": str(obj.get("title", ""))[:256],
        "draft_content": str(obj.get("content", ""))[:8000],
        "source_doc_ids": [d["doc_id"] for d in docs],
        "gap_evidence": evidence,
    }


async def _llm_user_links(db, tenant, queries):
    """Correlate evidence-gap fuel with durable LLM-user evaluation DomainEvents."""
    from app.models.domain_event import DomainEvent

    normalized = {str(query).strip() for query in queries if str(query).strip()}
    if not normalized:
        return []
    cutoff = utcnow() - timedelta(days=30)
    rows = (await db.execute(select(DomainEvent).where(
        DomainEvent.tenant_id == tenant,
        DomainEvent.event_type == "llm_user.eval.completed",
        DomainEvent.occurred_at >= cutoff,
    ).order_by(DomainEvent.occurred_at.desc()).limit(1000))).scalars().all()
    links = []
    seen = set()
    for event in rows:
        payload = event.payload or {}
        if str(payload.get("query", "")).strip() not in normalized:
            continue
        run_id = str(payload.get("runId", ""))
        scenario_version_id = str(payload.get("scenarioVersionId", ""))
        key = (run_id, scenario_version_id)
        if not all(key) or key in seen:
            continue
        seen.add(key)
        links.append({
            "runId": run_id,
            "scenarioVersionId": scenario_version_id,
            "eventId": event.id,
            "score": (payload.get("scores") or {}).get("outcome"),
        })
    return links[:20]


# ===== T6: run_scan 编排 + 入队 =====
async def _embed(queries):
    from app.services import embedding_service
    return await embedding_service.embed_texts(queries)


async def _retest_indexed_drafts(db, tenant):
    """B5 回流回测：对 status=indexed 且 indexed_at 早于 EVOLUTION_RETEST_AFTER_DAYS 的草稿
    重跑 member_queries 检索，对比回流前基线（gap_evidence_json.top1_score）算 lift，
    写回 gap_evidence_json。lift<=0 → quality_score *= 0.5（floor 0.05 防衰减到 0，回流无效降权）。

    单条异常 degraded("evo_retest_one", e) 不阻塞整批；外层查询异常 degraded("evo_retest_query", e)
    不中断 run_scan 后续 dislike 扫描；开关 EVOLUTION_RETEST_ENABLE 守关。返回参与回测的草稿数。
    """
    from app.config import settings
    from app.core import metrics

    if not getattr(settings, "EVOLUTION_RETEST_ENABLE", True):
        return 0
    try:
        after_days = int(getattr(settings, "EVOLUTION_RETEST_AFTER_DAYS", 7))
        cutoff = utcnow() - timedelta(days=after_days)
        rows = (await db.execute(
            select(KnowledgeEvolutionDraft).where(
                KnowledgeEvolutionDraft.tenant_id == tenant,
                KnowledgeEvolutionDraft.status == "indexed",
                KnowledgeEvolutionDraft.indexed_at.is_not(None),
                KnowledgeEvolutionDraft.indexed_at < cutoff,
            )
        )).scalars().all()
        retested = 0
        zero_recall_count = 0
        for r in rows:
            try:
                member_queries = json.loads(r.member_queries_json or "[]")
                if not member_queries:
                    continue   # 无样本可回测，跳过（非异常）
                evi = json.loads(r.gap_evidence_json or "{}")
                before = float(evi.get("top1_score") or 0.0)
                scores = []
                ai_doc_id = f"ai-evo-{tenant}"   # AI 草稿共享虚拟文档 ID
                for q in member_queries:
                    top = await _retrieve_top1(db, q, tenant, top_k=1)
                    if top and top[0].get("doc_id") == ai_doc_id:
                        # 只有 top1 命中的是 AI 草稿本身才算 lift，否则功劳不归本草稿
                        scores.append(float(top[0].get("score", 0.0) or 0.0))
                    # top1 命中人工文档 → AI 草稿无贡献，score 记 0
                after = round(sum(scores) / len(scores), 3) if scores else 0.0
                if after == 0.0:
                    zero_recall_count += 1
                lift = round(after - before, 3)
                evi["after_score"] = after
                evi["lift"] = lift
                evi["retested_at"] = utcnow().isoformat()
                r.gap_evidence_json = json.dumps(evi, ensure_ascii=False)
                if lift <= 0 and after > 0.0:
                    # 有召回但 lift≤0 → 回流确实无效，降权
                    # floor 0.05：防指数衰减到 0（spec §12 quality_score 仅信号，不可恢复可接受）
                    r.quality_score = max(round(r.quality_score * 0.5, 3), 0.05)
                # after==0.0（零召回）不降权——可能是检索故障而非草稿无效，防批量误判
                metrics.EVOLUTION_LIFT.observe(lift)
                retested += 1
            except Exception as e:
                degraded("evo_retest_one", e)
        # 批次级告警：零召回占比 > 50% 说明检索可能故障（非草稿无效）
        if retested and zero_recall_count / retested > 0.5:
            degraded("evo_retest_zero_recall_batch",
                     Exception(f"zero_recall={zero_recall_count}/{retested}"))
        if retested:
            await db.commit()
        return retested
    except Exception as e:
        # 外层查询异常（DB 连接 / 表结构等）→ 降级，不中断 run_scan 后续 dislike 扫描
        degraded("evo_retest_query", e)
        return 0


async def run_scan(db, tenant, *, since_hours=168, model_type=None):
    """全管道：抽取→聚类→盲区→草稿→落库(status=draft)。返回 {clusters, drafts}。

    B5：入口先回测已 indexed 的草稿（_retest_indexed_drafts），再扫新 dislike。
    """
    await _retest_indexed_drafts(db, tenant)
    queries = await _extract_dislike(db, tenant, since_hours)
    if not queries:
        return {"clusters": 0, "drafts": 0}
    vecs = await _embed(queries)
    items = [{"query": q, "vec": v} for q, v in zip(queries, vecs)]
    clusters = cluster(items)
    drafts = 0
    for c in clusters:
        evi = await _identify_blind_spot(db, c, tenant)
        if evi is None:
            continue
        d = await _generate_draft(db, c, evi, tenant, model_type)
        links = await _llm_user_links(db, tenant, [m["query"] for m in c["members"]])
        if links:
            evi["llm_user_links"] = links
        db.add(KnowledgeEvolutionDraft(
            id=uuid.uuid4().hex, tenant_id=tenant, cluster_id=c["cluster_id"],
            representative_query=c["representative_query"],
            member_queries_json=json.dumps([m["query"] for m in c["members"]], ensure_ascii=False),
            gap_evidence_json=json.dumps(evi, ensure_ascii=False),
            source_doc_ids_json=json.dumps(d["source_doc_ids"]),
            draft_title=d["draft_title"], draft_content=d["draft_content"],
            status="draft", quality_score=AI_QUALITY_SCORE, model_type=model_type or "",
        ))
        drafts += 1
    await db.commit()
    return {"clusters": len(clusters), "drafts": drafts}


async def enqueue_evolution_scan(tenant, *, since_hours=168, model_type=None):
    """入队扫描任务（复刻 governance.enqueue_governance_scan）。返回 task dict 或 None。"""
    from app.db.session import AsyncSessionLocal
    from app.services import task_queue_service
    async with AsyncSessionLocal() as db:
        task = await task_queue_service.enqueue_task_record(
            db, TASK_TYPE, {"since_hours": since_hours, "model_type": model_type},
            queue="default", idempotency_key=f"evo:{tenant}:{int(time.time()) // 300}",
            tenant_id=tenant, max_attempts=2, commit=True,
        )
    return task_queue_service.task_to_dict(task) if task else None


# ===== T8: 查询 + 审核 + 统计 =====
def _to_dict(r):
    return {
        "id": r.id, "clusterId": r.cluster_id, "representativeQuery": r.representative_query,
        "memberQueries": json.loads(r.member_queries_json or "[]"),
        "gapEvidence": json.loads(r.gap_evidence_json or "{}"),
        "sourceDocIds": json.loads(r.source_doc_ids_json or "[]"),
        "draftTitle": r.draft_title, "draftContent": r.draft_content,
        "status": r.status, "chunkId": r.chunk_id, "qualityScore": r.quality_score,
        "reviewer": r.reviewer, "reviewNote": r.review_note,
        "reviewedAt": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
        "indexedAt": r.indexed_at.isoformat() if r.indexed_at else None,
    }


async def list_drafts(db, tenant, *, status="", page=1, size=20):
    stmt = select(KnowledgeEvolutionDraft).where(KnowledgeEvolutionDraft.tenant_id == tenant)
    if status:
        stmt = stmt.where(KnowledgeEvolutionDraft.status == status)
    stmt = stmt.order_by(KnowledgeEvolutionDraft.created_at.desc()).offset((page - 1) * size).limit(size)
    rows = (await db.execute(stmt)).scalars().all()
    return {"total": len(rows), "list": [_to_dict(r) for r in rows]}


async def get_draft(db, draft_id, tenant):
    r = (await db.execute(
        select(KnowledgeEvolutionDraft).where(
            KnowledgeEvolutionDraft.id == draft_id, KnowledgeEvolutionDraft.tenant_id == tenant)
    )).scalar_one_or_none()
    return _to_dict(r) if r else None


async def review_draft(db, draft_id, tenant, *, action, note, reviewer):
    """审核：draft → approved(approve) / rejected(reject)。非 draft 不可审。"""
    r = (await db.execute(
        select(KnowledgeEvolutionDraft).where(
            KnowledgeEvolutionDraft.id == draft_id, KnowledgeEvolutionDraft.tenant_id == tenant)
    )).scalar_one_or_none()
    if not r:
        raise ValueError("草稿不存在")
    if r.status != "draft":
        raise ValueError(f"当前状态 {r.status} 不可审核")
    r.status = "approved" if action == "approve" else "rejected"
    r.reviewer = reviewer[:64]
    r.review_note = note[:500]
    r.reviewed_at = utcnow()
    await db.commit()
    return _to_dict(r)


async def get_stats(db, tenant):
    from collections import Counter
    rows = (await db.execute(
        select(KnowledgeEvolutionDraft).where(KnowledgeEvolutionDraft.tenant_id == tenant)
    )).scalars().all()
    c = Counter(r.status for r in rows)
    return {"byStatus": dict(c), "total": len(rows)}


# ===== T9: 回流 Milvus + 撤回 =====
# 虚拟 AI 文档 + Chunk/Milvus 持久化已迁移到 knowledge_backbone（供两套服务共用）
from app.services.knowledge_backbone import AI_DOC_NAME, ensure_ai_doc, persist_chunk  # noqa: F401


async def _persist_chunk_to_kb(db, draft):
    """写 Chunk 行 + embed + Milvus insert。返回 chunk_id。委托 backbone。"""
    return await persist_chunk(
        db, draft.draft_content or "", draft.draft_title or "", draft.tenant_id,
    )


async def _publish_llm_user_retests(draft):
    evidence = json.loads(draft.gap_evidence_json or "{}")
    from app.events.registry import publish_event

    for link in evidence.get("llm_user_links", [])[:20]:
        await publish_event(
            "knowledge_evolution.draft.indexed",
            {
                "draftId": draft.id,
                "runId": link.get("runId", ""),
                "scenarioVersionId": link.get("scenarioVersionId", ""),
                "tenantId": draft.tenant_id,
                "indexedAt": draft.indexed_at.isoformat() if draft.indexed_at else None,
            },
            source="knowledge-evolution",
            aggregate_type="knowledge_evolution_draft",
            aggregate_id=draft.id,
            tenant_id=draft.tenant_id,
            idempotency_key=f"llm-user-retest:{draft.id}:{link.get('runId', '')}",
            correlation_id=str(link.get("runId", "")),
        )


async def reflow_to_kb(db, draft):
    """approved → 回流(indexed)。幂等：已 indexed 直接返回 chunk_id，不重复写。"""
    if draft.status == "indexed":
        try:
            await _publish_llm_user_retests(draft)
        except Exception as exc:
            degraded("llm_user_evolution_event", exc, f"draft={draft.id}")
            raise
        return draft.chunk_id
    if draft.status != "approved":
        raise ValueError("仅 approved 可回流")
    # 配额：每周 indexed 上限（防回环污染；KNOWLEDGE_EVOLUTION_WEEKLY_QUOTA，0=不限）
    from app.config import settings
    quota = int(getattr(settings, "KNOWLEDGE_EVOLUTION_WEEKLY_QUOTA", WEEKLY_QUOTA_DEFAULT))
    if quota > 0:
        weekly = await _weekly_indexed_count(db, draft.tenant_id)
        if weekly >= quota:
            raise ValueError(f"本周回流配额已满({weekly}/{quota})")
    chunk_id = await _persist_chunk_to_kb(db, draft)
    draft.chunk_id = chunk_id
    draft.status = "indexed"
    draft.indexed_at = utcnow()
    await db.commit()
    from app.services.knowledge_change_service import knowledge_changed
    await knowledge_changed(f"ai-evo-{draft.tenant_id}", tenant_id=draft.tenant_id)
    try:
        await _publish_llm_user_retests(draft)
    except Exception as exc:
        degraded("llm_user_evolution_event", exc, f"draft={draft.id}")
        raise
    return chunk_id


async def _delete_chunk_from_milvus(chunk):
    """按 pk 删 Milvus 单条（AI 草稿共享 doc_id，不能用 delete_by_doc）。"""
    try:
        from pymilvus import Collection
        from app.clients import milvus_client
        col = Collection(milvus_client.primary_document_collection())
        col.delete(f'pk == "{chunk.doc_id}_{chunk.chunk_idx}"')
        col.flush()
    except Exception as e:
        degraded("evo_withdraw_milvus", e)


async def withdraw_draft(db, draft_id, tenant):
    """indexed → withdrawn：删 Chunk + Milvus 单条。"""
    r = (await db.execute(select(KnowledgeEvolutionDraft).where(
        KnowledgeEvolutionDraft.id == draft_id, KnowledgeEvolutionDraft.tenant_id == tenant)
    )).scalar_one_or_none()
    if not r:
        raise ValueError("草稿不存在")
    if r.status != "indexed":
        raise ValueError("仅 indexed 可撤回")
    from app.models.chunk import Chunk
    from app.models.document import Document
    chunk = (await db.execute(select(Chunk).where(Chunk.id == r.chunk_id))).scalar_one_or_none()
    doc_id = chunk.doc_id if chunk else f"ai-evo-{tenant}"
    doc = (await db.execute(
        select(Document).where(Document.id == doc_id, Document.tenant_id == tenant)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if chunk:
        await _delete_chunk_from_milvus(chunk)
        await db.execute(delete(Chunk).where(Chunk.id == r.chunk_id))
    if doc:
        doc.chunk_count = (await db.execute(
            select(func.count(Chunk.id)).where(Chunk.doc_id == doc_id).with_for_update()
        )).scalar_one()
    r.status = "withdrawn"
    await db.commit()
    from app.services.knowledge_change_service import knowledge_changed
    await knowledge_changed(doc_id, tenant_id=tenant)
    return _to_dict(r)


# ===== T10: 配额 + 定时入队 =====
async def _weekly_indexed_count(db, tenant):
    """本周(7天)已 indexed 草稿数（配额计数）。"""
    cutoff = utcnow() - timedelta(days=7)
    return (await db.execute(
        select(func.count()).select_from(KnowledgeEvolutionDraft).where(
            KnowledgeEvolutionDraft.tenant_id == tenant,
            KnowledgeEvolutionDraft.status == "indexed",
            KnowledgeEvolutionDraft.indexed_at >= cutoff,
        )
    )).scalar() or 0


async def evolution_cron_loop(tenant: str = "default"):
    """定时入队自进化扫描（lifespan 启动；周期=KNOWLEDGE_EVOLUTION_CRON_HOURS，<=0 关闭）。"""
    from app.config import settings
    interval = float(getattr(settings, "KNOWLEDGE_EVOLUTION_CRON_HOURS", 24))
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval * 3600)
        try:
            await enqueue_evolution_scan(tenant)
            print(f"[evolution] 定时扫描已入队(tenant={tenant})")
        except Exception as e:
            degraded("evo_cron_enqueue", e)
