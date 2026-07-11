"""Reciprocal Rank Fusion：融合多路检索的排名。

支持 per-list 权重（默认等权，向后兼容）：某路更可信（如电网术语 BM25 精确匹配）给更高权重。
"""


def rrf_fuse(lists: list[list[dict]], key_fn, k: int = 60, weights: list[float] | None = None) -> list[dict]:
    """lists: 多路检索结果；key_fn(hit) -> 唯一 chunk 标识。返回融合后按分数降序。

    weights: 与 lists 等长的 per-list 权重（默认全 1，等权）。某路更可信则给更高权重。
    """
    if weights is None:
        weights = [1.0] * len(lists)
    scores: dict = {}
    meta: dict = {}
    for w, hits in zip(weights, lists):
        for rank, h in enumerate(hits):
            key = key_fn(h)
            scores[key] = scores.get(key, 0.0) + w * 1.0 / (k + rank + 1)
            if key not in meta:
                meta[key] = h
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [{**meta[key], "score": round(s, 4)} for key, s in ranked]
