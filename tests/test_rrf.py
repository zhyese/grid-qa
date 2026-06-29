"""RRF 融合单测。"""
from app.rag.rrf import rrf_fuse


def test_empty():
    assert rrf_fuse([], key_fn=lambda h: h) == []


def test_fuse_ranking():
    dense = [{"key": 1}, {"key": 2}, {"key": 3}]
    sparse = [{"key": 2}, {"key": 4}]
    fused = rrf_fuse([dense, sparse], key_fn=lambda h: h["key"])
    keys = [h["key"] for h in fused]
    assert set(keys) == {1, 2, 3, 4}
    # key=2 在两路都靠前 → 融合后应排第一
    assert keys[0] == 2
    # 分数递减
    scores = [h["score"] for h in fused]
    assert scores == sorted(scores, reverse=True)
