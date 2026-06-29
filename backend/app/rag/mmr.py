"""MMR（Maximal Marginal Relevance）多样性重排：去冗余，让 topK 覆盖更全。

简化实现：用字符 2-gram Jaccard 衡量 chunk 间相似度（无需额外向量）。
"""
def _grams(text: str) -> set:
    t = text or ""
    return {t[i:i + 2] for i in range(max(0, len(t) - 1))}


def mmr(candidates: list[dict], topk: int, lambda_: float = 0.6) -> list[dict]:
    """candidates: [{text, score, ...}]（建议已按相关性降序）。返回 diverse topk。"""
    if len(candidates) <= topk:
        return candidates
    cg = [_grams(c.get("text", "")) for c in candidates]
    selected = [0]  # 先选最相关
    while len(selected) < topk:
        best_j, best_score = -1, -1e9
        for j in range(len(candidates)):
            if j in selected:
                continue
            rel = candidates[j].get("score", 0.0)
            div = max(
                len(cg[j] & cg[s]) / max(1, len(cg[j] | cg[s])) for s in selected
            )
            score = lambda_ * rel - (1 - lambda_) * div
            if score > best_score:
                best_score, best_j = score, j
        if best_j < 0:
            break
        selected.append(best_j)
    return [candidates[i] for i in selected]
