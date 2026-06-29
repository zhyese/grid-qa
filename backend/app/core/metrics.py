"""Prometheus 指标定义。"""
from prometheus_client import Counter, Histogram

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
