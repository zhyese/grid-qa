"""Corrective RAG 分级器单测（纯逻辑，阈值边界）。"""
from app.rag import crag


def test_grade_correct():
    g, _ = crag.grade(0.85, 5)
    assert g == crag.GRADE_CORRECT


def test_grade_incorrect_low_score():
    g, _ = crag.grade(0.1, 5)
    assert g == crag.GRADE_INCORRECT


def test_grade_incorrect_empty():
    g, _ = crag.grade(0.0, 0)
    assert g == crag.GRADE_INCORRECT


def test_grade_ambiguous_middle():
    g, _ = crag.grade(0.45, 5)  # 介于 LOW(0.3) 和 HIGH(0.6) 之间
    assert g == crag.GRADE_AMBIGUOUS


def test_grade_rerank_off_degrades_ambiguous():
    """rerank 未启用时 score 不可靠 → 降级 ambiguous，不误触发纠错。"""
    g, _ = crag.grade(0.95, 5, rerank_ok=False)
    assert g == crag.GRADE_AMBIGUOUS
    g2, _ = crag.grade(0.05, 5, rerank_ok=False)
    assert g2 == crag.GRADE_AMBIGUOUS


def test_confidence_mapping():
    assert crag.confidence_of(crag.GRADE_CORRECT, False) == "high"
    assert crag.confidence_of(crag.GRADE_INCORRECT, True) == "refused"
    assert crag.confidence_of(crag.GRADE_AMBIGUOUS, False) == "medium"
    assert crag.confidence_of(crag.GRADE_CORRECT, True) == "medium"  # 改写过→降为 medium
