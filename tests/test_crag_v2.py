"""CRAG v2 per-doc 分级单测（纯逻辑：相关性标签 → 分级）。"""
from app.rag import crag
from app.rag.crag_v2 import labels_to_grade


def test_two_relevant_is_correct():
    g, d = labels_to_grade(["relevant", "relevant", "irrelevant"])
    assert g == crag.GRADE_CORRECT
    assert d["relevant"] == 2


def test_one_relevant_is_ambiguous():
    assert labels_to_grade(["relevant", "irrelevant"])[0] == crag.GRADE_AMBIGUOUS


def test_one_partial_is_ambiguous():
    assert labels_to_grade(["partial", "irrelevant"])[0] == crag.GRADE_AMBIGUOUS


def test_all_irrelevant_is_incorrect():
    g, d = labels_to_grade(["irrelevant", "irrelevant", "irrelevant"])
    assert g == crag.GRADE_INCORRECT
    assert d["irrelevant"] == 3


def test_empty_is_incorrect():
    """无任何相关证据 → incorrect（触发 CRAG 纠错闭环）。"""
    assert labels_to_grade([])[0] == crag.GRADE_INCORRECT
