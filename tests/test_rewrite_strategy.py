"""RewriteStrategyClassifier 单测：类型分类 + few-shot + adaptive skip。"""
import pytest
from app.services.rewrite_strategy import classify, get_fewshot


def test_colloquial_short_query():
    """短 query 或含口语词 → colloquial，不跳过。"""
    r = classify("咋办")
    assert r["type"] == "colloquial"
    assert r["skip"] is False


def test_abbreviation():
    """含电网缩写 → abbreviation。"""
    r = classify("SF6断路器漏气怎么处理")
    assert r["type"] == "abbreviation"
    assert r["skip"] is False


def test_normal_skipped(monkeypatch):
    """规范 query（无口语/缩写/别名）→ normal，skip=True（兼 adaptive）。"""
    from app.services import term_service
    monkeypatch.setattr(term_service, "_load_terms", lambda: {})
    r = classify("主变压器绕组温度过热的应急处置步骤")
    assert r["type"] == "normal"
    assert r["skip"] is True


def test_empty_query_skipped():
    r = classify("")
    assert r["skip"] is True


def test_fewshot_returns_examples():
    fs = get_fewshot("colloquial")
    assert isinstance(fs, list) and len(fs) >= 1
