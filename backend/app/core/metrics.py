"""Prometheus 指标定义。"""
from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "grid_http_requests_total", "HTTP 请求总数", ["method", "path", "status"]
)
LATENCY = Histogram(
    "grid_http_latency_seconds", "HTTP 请求延迟(秒)", ["path"]
)
QA_TOTAL = Counter(
    "grid_qa_total", "问答请求总数", ["model", "cached"]
)
RETRIEVAL_LATENCY = Histogram(
    "grid_retrieval_latency_seconds", "检索延迟(秒)"
)
# 模型指标
LLM_CALLS = Counter(
    "grid_llm_calls_total", "LLM 调用次数", ["provider"]
)
LLM_LATENCY = Histogram(
    "grid_llm_latency_seconds", "LLM 调用延迟(秒)", ["provider"]
)
EMBED_CALLS = Counter(
    "grid_embed_calls_total", "Embedding 调用次数", ["provider"]
)
RERANK_CALLS = Counter(
    "grid_rerank_calls_total", "Rerank 调用次数"
)

# ===== 监控盲区补齐 =====
# 健康：系统错误（业务异常 BizError + HTTP 5xx）—— 原完全未埋
ERRORS = Counter("grid_errors_total", "系统错误总数", ["type", "code"])
# 延迟补齐（原仅有调用次数）
EMBED_LATENCY = Histogram("grid_embed_latency_seconds", "Embedding 调用延迟(秒)", ["provider"])
RERANK_LATENCY = Histogram("grid_rerank_latency_seconds", "Rerank 调用延迟(秒)")
# 质量：幻觉率分布 + 用户反馈
HALLUC = Histogram(
    "grid_hallucination_rate", "答案幻觉率分布",
    buckets=(0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, float("inf")),
)
FEEDBACK = Counter("grid_feedback_total", "问答反馈(👍/👎)", ["feedback"])
# 双 embedding 路由分布（云 vs 本地 bge）
VECTOR_ROUTE = Counter("grid_vector_route_total", "向量化路由", ["route"])
# 知识库规模（Gauge）
KB_DOCS = Gauge("grid_kb_docs", "知识库文档总数")
KB_CHUNKS = Gauge("grid_kb_chunks", "知识库分块总数")
KB_VECTORS = Gauge("grid_kb_vectors", "知识库向量总数")
# 知识图谱
KG_EXTRACT = Counter("grid_kg_extract_total", "知识图谱抽取次数")
KB_TRIPLES = Gauge("grid_kb_triples", "知识图谱三元组总数")
