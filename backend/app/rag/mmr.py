"""MMR（Maximal Marginal Relevance）多样性重排：去冗余，让 topK 覆盖更全。

用 jieba 分词后的 token Jaccard 衡量 chunk 间相似度（比字符 2-gram 更贴合中文术语：
"主变压器/变压器" 不再被误判完全冗余，"断路器/开关" 同义但 token 不重叠 → 走多样性）。
"""
from functools import lru_cache

import jieba


@lru_cache(maxsize=4096)
def _tokens(text: str) -> frozenset:
    """jieba 分词 → token 集（过滤单字/标点/空白）。frozenset 可哈希供 lru_cache。"""
    t = text or ""
    return frozenset(w for w in jieba.cut(t) if len(w.strip()) >= 2)


def mmr(candidates: list[dict], topk: int, lambda_: float = 0.5) -> list[dict]:
    """candidates: [{text, score, ...}]（建议已按相关性降序）。返回 diverse topk。"""
    if len(candidates) <= topk:
        return candidates
    cg = [_tokens(c.get("text", "")) for c in candidates]
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
