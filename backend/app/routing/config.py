"""路由配置：阈值/开关/权重。"""
from dataclasses import dataclass, field


@dataclass
class RouterConfig:
    """查询路由器配置。"""

    # ---- 全局开关 ----
    enabled: bool = True  # 总开关（关闭=全部走 hybrid，零影响）

    # ---- 稀疏路由阈值 ----
    sparse_max_len: int = 5  # ≤此长度的查询直接走 sparse
    sparse_term_density: float = 0.3  # 术语密度 ≥此值走 sparse（配合长度≤10）
    sparse_max_len_for_density: int = 10  # 术语密度判断的最大长度

    # ---- 稠密路由阈值 ----
    dense_min_len: int = 30  # ≥此长度的长自然语言走 dense
    dense_max_term_density: float = 0.1  # 术语密度 ≤此值的长句走 dense

    # ---- 跳过 Rerank 阈值 ----
    skip_rerank_confidence: float = 0.85  # 路由置信度 ≥此值时跳过 rerank

    # ---- 降级 ----
    min_confidence: float = 0.6  # 置信度 <此值自动升级到 hybrid
    sparse_fallback_min_score: float = 0.3  # BM25 top1 分数 <此值回退 hybrid

    # ---- 电网术语词典路径 ----
    term_dict_path: str = "backend/app/data/grid_terms.json"

    # ---- A/B 测试 ----
    ab_test_ratio: float = 1.0  # 走路由的流量比例（1.0=全量, 0.5=50%）

    @property
    def hybrid_routes(self) -> set[str]:
        return {"hybrid", "sparse_first"}


# 全局单例
router_config = RouterConfig()
