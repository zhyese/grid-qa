"""Reciprocal Rank Fusion：融合多路检索的排名。"""


def rrf_fuse(lists: list[list[dict]], key_fn, k: int = 60) -> list[dict]:
    """lists: 多路检索结果；key_fn(hit) -> 唯一 chunk 标识。返回融合后按分数降序。"""
    scores: dict = {}
    meta: dict = {}
    for hits in lists:
        for rank, h in enumerate(hits):
            key = key_fn(h)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            if key not in meta:
                meta[key] = h
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [{**meta[key], "score": round(s, 4)} for key, s in ranked]
