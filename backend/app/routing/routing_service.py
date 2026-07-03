"""路由调度服务：封装 classify → validate → fallback 链路。

集成点: qa_service.py 在 mixed_search() 前调用 route_query()
"""
import time
from typing import Optional

from app.core.obs import degraded
from app.routing.config import router_config
from app.routing.query_classifier import (
    QueryFeatures,
    RoutingDecision,
    classify,
    should_skip_rerank,
)


def route_query(query: str) -> RoutingDecision:
    """主入口：对查询执行路由决策。

    返回 RoutingDecision，其中 route ∈ {sparse, dense, hybrid, sparse_first}。
    调用方根据 decision.route 选择检索路径:
      - sparse       → 仅 BM25
      - dense        → 仅向量（双 collection）
      - sparse_first → 先 BM25，top1 分数低则回退 hybrid
      - hybrid       → 全链路（现有行为）
    """
    if not router_config.enabled:
        return RoutingDecision("hybrid", 1.0, "路由总开关关闭", None, False)

    t0 = time.time()
    try:
        decision = classify(query)
    except Exception as e:
        degraded("router_classify", e)
        decision = RoutingDecision("hybrid", 1.0, f"分类异常回退: {e}", None, False)

    # 置信度不足 → 升级到 hybrid（安全兜底）
    if decision.confidence < router_config.min_confidence:
        decision = RoutingDecision(
            "hybrid", 1.0,
            f"置信度不足({decision.confidence:.2f}<{router_config.min_confidence}), 回退全链路",
            decision.features, False,
        )

    # 记录指标
    try:
        from app.core import metrics
        metrics.ROUTING_DECISION.labels(decision.route).inc()
        metrics.ROUTING_LATENCY.observe(time.time() - t0)
    except Exception:
        pass

    return decision


def validate_sparse_result(decision: RoutingDecision, top1_score: float) -> bool:
    """sparse_first 路由：检查 BM25 top1 分数是否足够。"""
    return top1_score >= router_config.sparse_fallback_min_score


def log_routing_mismatch(decision: RoutingDecision, actual_route: str, detail: str = "") -> None:
    """记录路由与实际执行路径不一致的情况（供 Phase C 训练数据收集）。"""
    try:
        from app.core import metrics
        metrics.ROUTING_MISMATCH.labels(
            f"{decision.route}->{actual_route}"
        ).inc()
    except Exception:
        pass
    if detail:
        degraded("routing_mismatch", Exception(detail),
                 f"expected={decision.route} actual={actual_route}")
