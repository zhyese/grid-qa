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
# 降级：业务/IO 失败被兜底吞掉时的计数（Neo4j挂/rerank挂/缓存挂/删除失败…）—— 让盲降级可见
DEGRADED = Counter("grid_degraded_total", "静默降级次数(失败被兜底)", ["tag"])
# Corrective RAG：检索结果分级（correct/ambiguous/incorrect）+ 纠错动作
CRAG_GRADE = Counter("grid_crag_grade_total", "CRAG 检索分级", ["grade"])
CRAG_ACTION = Counter("grid_crag_action_total", "CRAG 纠错动作", ["action"])
# 基础组件健康（MySQL/Milvus/Redis/MinIO 探活结果，1=up/0=down）—— 让 /health 状态进 Grafana 可监控可告警
COMPONENT_HEALTH = Gauge("grid_component_health", "基础组件健康状态(1=up/0=down)", ["component"])
# 安全合规：prompt injection 命中 + 答案脱敏次数（D4，电网强监管可见性）
SAFETY_BLOCK = Counter("grid_safety_block_total", "安全事件(prompt injection/敏感信息脱敏)", ["kind"])
# 领域增强：故障诊断 / 两票生成 / 相似案例 调用次数（D1/D2/D3）
DOMAIN_CALLS = Counter("grid_domain_calls_total", "领域增强调用", ["feature"])
