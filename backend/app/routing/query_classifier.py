"""查询特征提取 + 决策树分类器。

提取 6 维特征 → 决策树 → RoutingDecision(route, confidence, reason)
"""
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

# ---- 电网术语词典（约 50 核心词 + grid_terms.json 别名）----
_CORE_TERMS: set[str] = {
    # 一次设备
    "变压器", "主变", "断路器", "隔离开关", "接地刀闸", "互感器",
    "电流互感器", "电压互感器", "避雷器", "GIS", "SF6", "开关柜",
    "电缆", "架空线", "母线", "电容器", "电抗器", "消弧线圈",
    # 二次设备
    "继电保护", "自动装置", "远动", "测控", "合并单元", "智能终端",
    "差动保护", "瓦斯保护", "过流保护", "距离保护", "零序保护",
    "重合闸", "备自投", "安稳装置", "故障录波",
    # 运维术语
    "跳闸", "合闸", "分闸", "倒闸", "停电", "送电", "接地",
    "绝缘", "耐压", "局放", "油色谱", "红外测温", "带电检测",
    "巡检", "检修", "预试", "定检", "消缺", "抢修",
    # 参数单位
    "kV", "kA", "MW", "MVA", "Hz", "Ω", "dB", "ppm",
    "r/min", "mm²", "MPa", "μΩ", "mΩ",
}

# ---- 标准引用正则 ----
_STANDARD_RE = re.compile(
    r"(DL\s*/\s*T\s*\d+|GB\s*/\s*T\s*\d+|Q\s*/\s*GDW\s*\d+|"
    r"JB\s*/\s*T\s*\d+|IEEE\s*\d+|IEC\s*\d+)",
    re.IGNORECASE,
)

# ---- 数值+单位正则 ----
_NUMERIC_RE = re.compile(
    r"\d+\.?\d*\s*(kV|kA|MW|MVA|Hz|Ω|dB|ppm|r/min|mm²|MPa|μΩ|mΩ|"
    r"℃|°C|kVAR|kV·A|kW·h|MWh|GWh|A|V|W|mH|μF|nF|pF|kΩ|MΩ|"
    # 中文单位（电网常用）
    r"千伏|千安|兆瓦|兆伏安|赫兹|欧姆|兆欧|微欧|毫欧|"
    r"摄氏度|千乏|千瓦时|兆瓦时|千伏安|安培|伏特|瓦特|"
    r"毫亨|微法|纳法|皮法|千欧|兆帕|平方毫米)",
    re.IGNORECASE,
)

# ---- 故障口语特征词 ----
_FAULT_WORDS = re.compile(
    r"故障|跳闸|事故|异常|告警|报警|烧毁|击穿|放电|闪络|短路|"
    r"接地|断线|缺相|过负荷|过流|过压|欠压|低频|高频|振荡|"
    r"冒烟|着火|爆炸|漏油|漏气|进水|受潮|锈蚀|卡涩|拒动|误动"
)

# ---- 自然语言特征词 ----
_NATURAL_WORDS = re.compile(
    r"的|是|了|吗|怎么|为什么|如何|怎样|什么|哪些|哪个|"
    r"请问|帮忙|能否|可否|应该|需要|可以|是否"
)


@dataclass
class QueryFeatures:
    """查询的 6 维特征。"""
    query_length: int = 0
    term_density: float = 0.0
    query_type: str = "keyword"       # keyword | natural | fault | mixed
    has_standard_reference: bool = False
    has_numeric_param: bool = False
    has_synonym_alias: bool = False


@dataclass
class RoutingDecision:
    """路由决策结果。"""
    route: str = "hybrid"             # sparse | dense | hybrid | sparse_first
    confidence: float = 0.5           # 0-1，越高越确信
    reason: str = ""
    features: Optional[QueryFeatures] = None
    skip_rerank: bool = False         # 是否可跳过 rerank


def _load_term_dict() -> set[str]:
    """从 grid_terms.json 加载别名，合并到核心术语集。"""
    terms = set(_CORE_TERMS)
    try:
        # 尝试多个路径
        for p in [
            os.path.join(os.path.dirname(__file__), "..", "data", "grid_terms.json"),
            "backend/app/data/grid_terms.json",
            "app/data/grid_terms.json",
        ]:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                for k, v in d.items():
                    terms.add(k)
                    terms.add(v)
                break
    except Exception:
        pass
    return terms


# 模块加载时构建（一次）
_TERM_DICT: set[str] = _load_term_dict()


def extract_features(query: str) -> QueryFeatures:
    """从查询文本提取 6 维特征（纯规则，<1ms）。"""
    f = QueryFeatures()
    text = query.strip()
    f.query_length = len(text)

    # 查询类型
    has_fault = bool(_FAULT_WORDS.search(text))
    has_natural = bool(_NATURAL_WORDS.search(text))
    if has_fault and has_natural:
        f.query_type = "fault"
    elif has_fault:
        f.query_type = "fault"
    elif has_natural:
        f.query_type = "natural"
    elif f.query_length <= 10:
        f.query_type = "keyword"
    else:
        f.query_type = "mixed"

    # 术语密度
    words = set(text)  # 字符级，对中文更适用（避免分词差异）
    term_hits = sum(1 for t in _TERM_DICT if t in text)
    f.term_density = min(term_hits / max(len(text), 1), 1.0)

    # 布尔特征
    f.has_standard_reference = bool(_STANDARD_RE.search(text))
    f.has_numeric_param = bool(_NUMERIC_RE.search(text))
    f.has_synonym_alias = _check_synonym(text)

    return f


def _check_synonym(text: str) -> bool:
    """检查 query 中是否包含已知同义词/别名（如"主变""CT""刀闸"）。"""
    aliases = {
        "主变", "变压气", "高变", "站用变", "接地变",
        "刀闸", "CT", "TA", "PT", "TV", "GIS", "MOA",
        "重合闸", "备自投", "差动保护", "瓦斯保护", "有载调压",
    }
    return any(a in text for a in aliases)


def classify(query: str) -> RoutingDecision:
    """决策树：查询特征 → 路由路径。

    优先级：精确匹配 > 语义理解 > 混合兜底
    置信度 < min_confidence → 自动升级到 hybrid
    """
    from .config import router_config as cfg

    f = extract_features(query)

    # ---- 精确匹配优先 → sparse ----
    if f.has_standard_reference:
        return RoutingDecision("sparse", 0.92, "标准引用需精确字符串匹配", f, True)

    if f.query_length <= cfg.sparse_max_len:
        return RoutingDecision("sparse", 0.90, f"超短查询({f.query_length}字) IDF 已足够精准", f, True)

    if f.query_length <= cfg.sparse_max_len_for_density and f.term_density >= cfg.sparse_term_density:
        return RoutingDecision("sparse", 0.85,
                              f"术语密集型短查询(密度{f.term_density:.2f})", f, True)

    if f.has_numeric_param:
        return RoutingDecision("sparse_first", 0.72,
                              "含数值参数, 先 sparse 后按需 hybrid", f, False)

    # ---- 语义理解优先 → dense ----
    if f.query_type in ("fault",) and f.query_length > 10:
        return RoutingDecision("dense", 0.88, "故障口语化描述需语义抽象", f, False)

    if f.has_synonym_alias and f.query_length > 5:
        return RoutingDecision("dense", 0.78, "同义词/别名混用需隐式语义近邻", f, False)

    if f.query_length >= cfg.dense_min_len and f.term_density <= cfg.dense_max_term_density:
        return RoutingDecision("dense", 0.82,
                              f"长自然语言低术语密度(密度{f.term_density:.2f})", f, False)

    # ---- 默认兜底 → hybrid ----
    return RoutingDecision("hybrid", 0.50, "规则未覆盖, 走全链路确保召回", f, False)


def should_skip_rerank(decision: RoutingDecision) -> bool:
    """判断是否可跳过 rerank（高置信度 + 稀疏路由）。"""
    from .config import router_config as cfg
    return (
        decision.skip_rerank
        and decision.confidence >= cfg.skip_rerank_confidence
        and decision.route in ("sparse",)
    )
