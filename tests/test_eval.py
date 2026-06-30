"""评测集覆盖度测试（S1：golden 扩容 + 场景覆盖）。"""
import json
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent.parent / "backend" / "data" / "golden_qa.json"


def _load():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_golden_expanded_to_30_plus():
    """golden 集扩容到 ≥30 条（原 12 条太少，门禁无统计意义）。"""
    items = _load()
    assert len(items) >= 30


def test_golden_covers_three_scenes():
    """覆盖变电/配电/输电三大场景。"""
    cats = {it["category"] for it in _load()}
    assert {"变电", "配电", "输电"}.issubset(cats)


def test_golden_fields_complete():
    """每条 query/expect/category 非空（与 validate_golden.py CI 校验一致）。"""
    for it in _load():
        assert it.get("query") and isinstance(it["query"], str)
        assert it.get("expect") and isinstance(it["expect"], list)
        assert it.get("category")


def test_golden_no_duplicate_query():
    """无重复问题（避免回流 golden 时膨胀重复条目）。"""
    queries = [it["query"].strip() for it in _load()]
    assert len(queries) == len(set(queries))
