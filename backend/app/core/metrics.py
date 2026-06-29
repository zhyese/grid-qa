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
