"""检索评测纯函数单测：recall@k / MRR / nDCG（已知 golden→已知值）。"""
from app.services.retrieval_eval_service import _recall_at_k, _mrr, _ndcg


def test_recall_at_k_all_hit():
    expect = ["主变运维手册.pdf", "故障案例.docx"]
    got = ["其他.doc", "主变运维手册.pdf", "故障案例.docx"]
    assert _recall_at_k(expect, got) == 1.0


def test_recall_at_k_partial():
    assert _recall_at_k(["A", "B"], ["A", "C"]) == 0.5


def test_recall_at_k_none():
    assert _recall_at_k(["A"], ["B", "C"]) == 0.0


def test_recall_at_k_empty_expect():
    assert _recall_at_k([], ["A"]) == 0.0


def test_mrr_first_rank():
    assert _mrr(["B"], ["B", "A"]) == 1.0


def test_mrr_second_rank():
    assert _mrr(["B"], ["A", "B", "C"]) == 0.5


def test_mrr_no_hit():
    assert _mrr(["Z"], ["A", "B"]) == 0.0


def test_ndcg_ideal_order():
    # 最优排序（高相关在前）→ nDCG=1.0
    rd = {"高.pdf": 3, "低.pdf": 1}
    got = ["高.pdf", "低.pdf"]
    assert abs(_ndcg(rd, got) - 1.0) < 1e-6


def test_ndcg_suboptimal():
    rd = {"高.pdf": 3, "低.pdf": 1}
    got = ["低.pdf", "高.pdf"]  # 低相关在前 → < 1.0
    assert 0 < _ndcg(rd, got) < 1.0


def test_ndcg_no_relevant():
    assert _ndcg({"A": 3}, ["B", "C"]) == 0.0
