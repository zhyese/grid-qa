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
        assert isinstance(it.get("expect"), list)
        assert it.get("category")


def test_golden_no_duplicate_query():
    """无重复问题（避免回流 golden 时膨胀重复条目）。"""
    queries = [it["query"].strip() for it in _load()]
    assert len(queries) == len(set(queries))


def test_eval_citation_smoke(monkeypatch):
    """eval_citation 四样本跑通（mock verify 不打真 API），退出码语义正确。"""
    import asyncio
    import importlib.util
    from pathlib import Path

    async def fake_verify(*a, **kw):
        from app.schemas.citation import VerifyItem, VerifyResult
        return VerifyResult(items=[VerifyItem(ref_id=1, chunk_id="c1", valid=True,
                                              nli_label="support", action="keep")])

    monkeypatch.setattr("app.rag.citation_verifier.verify", fake_verify)

    spec = importlib.util.spec_from_file_location(
        "eval_citation", Path(__file__).resolve().parent.parent / "scripts" / "eval_citation.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    rep = asyncio.new_event_loop().run_until_complete(mod.evaluate(0.8, nli_enable=True))
    assert rep["metrics"]["association"] >= 0.8
    assert rep["metrics"]["pass"] is True
    assert len(rep["samples"]) == 4
