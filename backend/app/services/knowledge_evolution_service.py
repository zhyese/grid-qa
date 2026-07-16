"""知识库自进化编排器：dislike 聚类 → 盲区 → LLM 草稿 → 审核回流。

复刻 knowledge_governance 的 scan 入队 + review 审核双范式。
生长顺序：T3 聚类 → T4 抽取/盲区 → T5 草稿 → T6 编排 → T8 审核 → T9 回流。
"""
import json
import math
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.models.evidence_gap import EvidenceGap
from app.models.feedback import Feedback
from app.services.task_queue_service import utcnow

# ===== 常量（spec global constraints）=====
TASK_TYPE = "knowledge_evolution.scan"
CLUSTER_THRESHOLD = 0.82       # 余弦相似度归簇阈值
CLUSTER_MIN_SIZE = 3           # 簇内最少 dislike 条数才算高频盲区候选
BLIND_TOP1_THRESHOLD = 0.55    # 检索 top1 score 低于此 = 盲区
AI_QUALITY_SCORE = 0.6         # AI 生成 chunk 质量分(<人工 1.0)，检索降权
WEEKLY_QUOTA_DEFAULT = 20      # 每周回流配额


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
    """检索 top-k，归一为 [{score, doc_id}]。mixed_search 返回 item 用 docId/score。"""
    from app.services import retrieval_service
    items = await retrieval_service.mixed_search(db, query, topk=top_k, tenant=tenant)
    return [{
        "score": float(it.get("score", 0.0) or 0.0),
        "doc_id": it.get("docId", "") or it.get("doc_id", ""),
    } for it in items]


async def _extract_dislike(db, tenant, since_hours):
    """拉 since_hours 内 dislike query（Feedback）+ EvidenceGap pending，去重。

    注：feedback_service.list_feedbacks 无 tenant/since 参数，故直接查 Feedback 表。
    """
    cutoff = utcnow() - timedelta(hours=since_hours)
    stmt = select(Feedback.query).where(Feedback.feedback == "dislike", Feedback.created_at >= cutoff)
    if hasattr(Feedback, "tenant_id"):           # Feedback 可能无 tenant_id（历史单租户），有则过滤
        stmt = stmt.where(Feedback.tenant_id == tenant)
    rows = (await db.execute(stmt)).scalars().all()
    queries = [q.strip() for q in rows if q and q.strip()]
    seen = set(queries)
    gaps = (await db.execute(
        select(EvidenceGap.query).where(EvidenceGap.status == "pending", EvidenceGap.tenant_id == tenant)
    )).scalars().all()
    for q in gaps:
        q = (q or "").strip()
        if q and q not in seen:
            seen.add(q)
            queries.append(q)
    return queries


async def _identify_blind_spot(db, cluster_obj, tenant):
    """簇代表 query 检索 top1；score < 阈值 = 盲区返回证据，否则 None。"""
    top = await _retrieve_top1(db, cluster_obj["representative_query"], tenant, top_k=1)
    if not top:
        return {"top1_score": 0.0, "hit_doc_ids": [], "confidence": "blind"}
    score = top[0]["score"]
    if score >= BLIND_TOP1_THRESHOLD:
        return None
    return {"top1_score": score, "hit_doc_ids": [top[0]["doc_id"]], "confidence": "medium"}
