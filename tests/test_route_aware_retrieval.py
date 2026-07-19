"""Batch 1 routing-aware 调参单测（A2/A3/A5/B6）。

只测 mixed_search 内的纯 helper 函数（_rrf_weights_for_route /
_mmr_lambda_for_query_type / _ef_for_route / _rerank_pool_cap）+
config 默认值——不依赖 db/Milvus/embedding/rerank，稳定。

mixed_search 实际检索行为由集成测试覆盖（同 test_mixed_search_overrides 范式）。
"""
import math


# ---------- A2 · RRF 动态加权 ----------

def test_rrf_weights_hybrid_equal():
    """hybrid 路径等权（=现状）。"""
    from app.services.retrieval_service import _rrf_weights_for_route
    assert _rrf_weights_for_route("hybrid", 1.0, 1.0) == (1.0, 1.0)


def test_rrf_weights_sparse_first_leans_sparse():
    """sparse_first：BM25 路加权 1.3（query IDF 强，稀疏更可信）。"""
    from app.services.retrieval_service import _rrf_weights_for_route
    dw, sw = _rrf_weights_for_route("sparse_first", 1.0, 1.0)
    assert dw == 1.0
    assert sw == 1.3


def test_rrf_weights_dense_leans_dense():
    """dense 路径：dense 加权 1.3。"""
    from app.services.retrieval_service import _rrf_weights_for_route
    dw, sw = _rrf_weights_for_route("dense", 1.0, 1.0)
    assert dw == 1.3
    assert sw == 1.0


def test_rrf_weights_preserves_custom_base():
    """base 非默认值时按 base 缩放（不锁定 1.0）。"""
    from app.services.retrieval_service import _rrf_weights_for_route
    dw, sw = _rrf_weights_for_route("sparse_first", 0.8, 1.2)
    assert dw == 0.8
    assert sw == round(1.2 * 1.3, 6)


def test_rrf_weights_unknown_route_equal():
    """未知 route 值 → 等权（兜底=现状）。"""
    from app.services.retrieval_service import _rrf_weights_for_route
    assert _rrf_weights_for_route("nonsense", 1.0, 1.0) == (1.0, 1.0)


# ---------- A3 · MMR λ 动态 ----------

def test_mmr_lambda_fault_precision():
    """fault → 0.7（精度优先，避免去重吞掉关键处置步骤）。"""
    from app.services.retrieval_service import _mmr_lambda_for_query_type
    assert _mmr_lambda_for_query_type("fault") == 0.7


def test_mmr_lambda_mixed_balanced():
    """mixed → 0.5（均衡）。"""
    from app.services.retrieval_service import _mmr_lambda_for_query_type
    assert _mmr_lambda_for_query_type("mixed") == 0.5


def test_mmr_lambda_natural_diverse():
    """natural → 0.4（多样性，表述模糊广撒网）。"""
    from app.services.retrieval_service import _mmr_lambda_for_query_type
    assert _mmr_lambda_for_query_type("natural") == 0.4


def test_mmr_lambda_unknown_returns_none():
    """未知 query_type / keyword → None（保留 settings.MMR_LAMBDA 现状）。"""
    from app.services.retrieval_service import _mmr_lambda_for_query_type
    assert _mmr_lambda_for_query_type("keyword") is None
    assert _mmr_lambda_for_query_type("unknown") is None


def test_mmr_lambda_none_returns_none():
    """query_type=None（routing 关 / features 缺）→ None。"""
    from app.services.retrieval_service import _mmr_lambda_for_query_type
    assert _mmr_lambda_for_query_type(None) is None


# ---------- A5 · rerank 早剪枝 ----------

def test_rerank_pool_cap_disabled_topk_x2():
    """route_aware=False → topk*2（现状）。"""
    from app.services.retrieval_service import _rerank_pool_cap
    assert _rerank_pool_cap(topk=10, route_aware=False) == 20


def test_rerank_pool_cap_enabled_topk_x12():
    """route_aware=True → ceil(topk*1.2)（省 rerank 额度）。"""
    from app.services.retrieval_service import _rerank_pool_cap
    assert _rerank_pool_cap(topk=10, route_aware=True) == 12


def test_rerank_pool_cap_ceil_fractional():
    """topk 不能被 1.2 整除时向上取整（不丢候选）。"""
    from app.services.retrieval_service import _rerank_pool_cap
    assert _rerank_pool_cap(topk=5, route_aware=True) == math.ceil(5 * 1.2)  # 6
    assert _rerank_pool_cap(topk=7, route_aware=True) == math.ceil(7 * 1.2)  # 9


def test_rerank_pool_cap_floor_one():
    """极端 topk=1 仍 ≥1（不空池）。"""
    from app.services.retrieval_service import _rerank_pool_cap
    assert _rerank_pool_cap(topk=1, route_aware=True) >= 1
    assert _rerank_pool_cap(topk=1, route_aware=False) >= 1


# ---------- B6 · Milvus ef 动态 ----------

def test_ef_hybrid_unchanged():
    """hybrid → base_ef（现状）。"""
    from app.services.retrieval_service import _ef_for_route
    assert _ef_for_route("hybrid", "mixed", 64) == 64


def test_ef_sparse_first_halved():
    """sparse/sparse_first → ef 减半（精确匹配场景不需高 ef）。"""
    from app.services.retrieval_service import _ef_for_route
    assert _ef_for_route("sparse_first", "keyword", 64) == 32
    assert _ef_for_route("sparse", "keyword", 64) == 32


def test_ef_dense_fault_doubled():
    """dense + fault → ef 翻倍（口语化故障需更高召回）。"""
    from app.services.retrieval_service import _ef_for_route
    assert _ef_for_route("dense", "fault", 64) == 128


def test_ef_dense_non_fault_unchanged():
    """dense + 非 fault → base_ef（只对故障提 ef）。"""
    from app.services.retrieval_service import _ef_for_route
    assert _ef_for_route("dense", "natural", 64) == 64
    assert _ef_for_route("dense", None, 64) == 64


def test_ef_floor_minimum():
    """小 base_ef 仍 ≥ 16（HNSW ef 太低会崩精度）。"""
    from app.services.retrieval_service import _ef_for_route
    assert _ef_for_route("sparse_first", "keyword", 8) == 16
    assert _ef_for_route("sparse_first", "keyword", 20) == 16  # 20//2=10 < 16 → 16


# ---------- 总开关默认关（保护现状）----------

def test_route_aware_disabled_by_default():
    """RRF_ROUTE_AWARE_ENABLE 默认 False（关时 mixed_search 逐字节=现状）。"""
    from app.config import Settings
    assert Settings().RRF_ROUTE_AWARE_ENABLE is False


def test_mixed_search_accepts_routing_decision_param():
    """mixed_search 签名仍接受 routing_decision（前端零改动，caller 不破坏）。"""
    import inspect
    from app.services.retrieval_service import mixed_search
    sig = inspect.signature(mixed_search)
    assert "routing_decision" in sig.parameters
    assert sig.parameters["routing_decision"].default is None
