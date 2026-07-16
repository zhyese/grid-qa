"""知识库自进化编排器：dislike 聚类 → 盲区 → LLM 草稿 → 审核回流。

复刻 knowledge_governance 的 scan 入队 + review 审核双范式。
本模块随 task 逐步生长：T3 聚类 → T4 抽取/盲区 → T5 草稿 → T6 编排 → T8 审核 → T9 回流。
"""
import math
import uuid

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
    """零依赖贪心近邻聚类。

    items: [{query, vec}]；返回 [{cluster_id, representative_query, members, centroid}]。
    簇内 < min_size 的丢弃；representative_query = 离质心最近的成员。
    """
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
            clusters.append({
                "cluster_id": uuid.uuid4().hex[:12],
                "centroid": list(it["vec"]),
                "members": [it],
            })
    out = []
    for c in clusters:
        if len(c["members"]) < min_size:
            continue
        rep = max(c["members"], key=lambda m: _cosine(m["vec"], c["centroid"]))
        c["representative_query"] = rep["query"]
        out.append(c)
    return out
