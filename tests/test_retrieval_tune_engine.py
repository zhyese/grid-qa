"""扫描引擎单测：PARAM_SPACE/SWITCHES 内容 + _build_suggestions 四道护栏。"""


def test_param_space_covers_key_params():
    from app.services.retrieval_tune_service import PARAM_SPACE, SWITCHES
    assert {"RRF_K", "MMR_LAMBDA", "RRF_DENSE_WEIGHT", "TOPK"}.issubset(PARAM_SPACE.keys())
    # 执行时验证：CRAG 不在 mixed_search 评测路径，已移除；HYDE 未透传 overrides，已移除
    assert "CRAG_HIGH" not in PARAM_SPACE
    assert "HYDE_ENABLE" not in SWITCHES
    assert "RERANK_ENABLE" in SWITCHES
    assert "MULTI_QUERY_ENABLE" in SWITCHES


def test_build_suggestions_filters_below_margin():
    from app.services.retrieval_tune_service import _build_suggestions
    from app.config import settings
    baseline = {"recall": 0.90, "mrr": 0.85, "ndcg": 0.88}
    scan = [
        {"param": "RRF_K", "value": 40, "current": 60, "recall": 0.905, "mrr": 0.85, "ndcg": 0.88},  # +0.005 < margin
        {"param": "RRF_K", "value": 80, "current": 60, "recall": 0.93, "mrr": 0.86, "ndcg": 0.89},   # +0.03 ≥ margin
    ]
    suggestions = _build_suggestions(baseline, scan, settings.TUNE_MIN_IMPROVE)
    assert len(suggestions) == 1            # 低提升被过滤，只留 RRF_K=80
    assert suggestions[0]["param"] == "RRF_K"
    assert suggestions[0]["suggested"] == 80


def test_build_suggestions_confidence_high_when_multi_metric_up():
    from app.services.retrieval_tune_service import _build_suggestions
    baseline = {"recall": 0.80, "mrr": 0.75, "ndcg": 0.78}
    scan = [{"param": "MMR_LAMBDA", "value": 0.3, "current": 0.5,
             "recall": 0.86, "mrr": 0.80, "ndcg": 0.84}]  # recall+0.06≥0.05 & mrr↑
    suggestions = _build_suggestions(baseline, scan, 0.02)
    assert suggestions[0]["confidence"] == "high"


def test_build_suggestions_low_when_mrr_drops():
    from app.services.retrieval_tune_service import _build_suggestions
    baseline = {"recall": 0.80, "mrr": 0.80, "ndcg": 0.78}
    scan = [{"param": "TOPK", "value": 8, "current": 5,
             "recall": 0.85, "mrr": 0.70, "ndcg": 0.80}]  # recall↑ mrr↓
    suggestions = _build_suggestions(baseline, scan, 0.02)
    assert suggestions[0]["confidence"] == "low"


def test_build_suggestions_no_param_below_margin():
    from app.services.retrieval_tune_service import _build_suggestions
    baseline = {"recall": 0.90, "mrr": 0.85, "ndcg": 0.88}
    scan = [{"param": "RRF_K", "value": 40, "current": 60, "recall": 0.901, "mrr": 0.85, "ndcg": 0.88}]
    assert _build_suggestions(baseline, scan, 0.02) == []
