"""MMR 多样性重排单测：jieba token 相似度 + lambda 权衡（点1）。"""
from app.rag.mmr import mmr, _tokens


def test_tokens_filters_single_char_and_keeps_terms():
    # 单字/标点过滤，术语保留
    assert "的" not in _tokens("主变压器的温度")
    assert "变压器" in _tokens("主变压器温度异常")


def test_mmr_returns_all_when_below_topk():
    cands = [{"text": "变压器温度异常", "score": 0.9}, {"text": "断路器跳闸", "score": 0.8}]
    assert len(mmr(cands, topk=5)) == 2


def test_mmr_prefers_diverse_over_redundant():
    # 候选1/2 高度重复，候选3 不同主题 → 第2个应选 diverse 的3
    cands = [
        {"text": "变压器温度异常处置流程步骤", "score": 0.95, "id": 1},
        {"text": "变压器温度异常处置流程", "score": 0.93, "id": 2},
        {"text": "断路器SF6气体泄漏处理方法", "score": 0.90, "id": 3},
    ]
    out = mmr(cands, topk=2, lambda_=0.5)
    ids = [c["id"] for c in out]
    assert 1 in ids and 3 in ids, f"期望选 diverse(1,3)，实际 {ids}"


def test_mmr_lambda_near_one_is_pure_relevance():
    # lambda→1 退化为纯分数序（不去冗余）
    cands = [
        {"text": "变压器温度异常", "score": 0.95, "id": 1},
        {"text": "变压器温度升高", "score": 0.94, "id": 2},
        {"text": "断路器跳闸", "score": 0.90, "id": 3},
    ]
    out = mmr(cands, topk=2, lambda_=0.99)
    assert [c["id"] for c in out] == [1, 2]
