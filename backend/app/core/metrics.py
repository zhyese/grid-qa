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
KB_VECTORIZED_DOCS = Gauge("grid_kb_vectorized_docs", "已向量化文档数(status=vectorized)")
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
# 两票智能审核结果分布（pass/warn/fail）
TICKET_AUDIT = Counter("grid_ticket_audit_total", "两票智能审核结果", ["result"])
# Agentic 诊断：循环深度分布（观测 agent 调几轮工具）
AGENT_ITERS = Histogram("grid_agent_iters", "诊断 agent 循环深度(轮)",
                        buckets=(1, 2, 3, 4, 5, 6, float("inf")))
# 告警闭环：Grafana alerting webhook 回调接收到的告警数（按 severity）
ALERT_RECEIVED = Counter("grid_alert_received_total", "告警接收总数(Grafana回调)", ["severity"])


def init_metric_series() -> None:
    """预注册已知小基数业务指标的 0 值序列，让面板在事件发生前就“在场”显示 0。

    底层逻辑：prometheus_client 只在被 .inc()/.observe()/.set() 触碰后才输出
    某条 label 序列。事件驱动型指标(反馈/向量化路由/CRAG/领域/安全/组件健康)
    在正常工况可能长时间不触发 → /metrics 不含该序列 → Grafana 面板 “No data”，
    看起来像“没打通后端”。启动时把已知 label 组合预置为 0 即可消除该盲区。

    开放基数指标(ERRORS.code / DEGRADED.tag)不在后端预置(会产生虚假序列)，
    由 Grafana 面板的 `or vector(0)` 兜底；其余无 label 的 Gauge/Counter 自带
    默认 0 值子项，天然可见，无需在此处理。
    """
    try:
        # 反馈：👍/👎
        FEEDBACK.labels("like").inc(0)
        FEEDBACK.labels("dislike").inc(0)
        # 双 embedding 路由：云 vs 本地 bge
        VECTOR_ROUTE.labels("cloud").inc(0)
        VECTOR_ROUTE.labels("bge").inc(0)
        # CRAG 检索分级
        CRAG_GRADE.labels("correct").inc(0)
        CRAG_GRADE.labels("ambiguous").inc(0)
        CRAG_GRADE.labels("incorrect").inc(0)
        # CRAG 纠错动作
        CRAG_ACTION.labels("normal").inc(0)
        CRAG_ACTION.labels("rewritten").inc(0)
        CRAG_ACTION.labels("refused").inc(0)
        # 领域增强
        DOMAIN_CALLS.labels("diagnose").inc(0)
        DOMAIN_CALLS.labels("ticket").inc(0)
        DOMAIN_CALLS.labels("similar_case").inc(0)
        DOMAIN_CALLS.labels("safety_block").inc(0)
        DOMAIN_CALLS.labels("diagnose_agent").inc(0)
        # 两票审核结果（预注册 0 值，消除面板 No data 盲区）
        for _res in ("pass", "warn", "fail"):
            TICKET_AUDIT.labels(_res).inc(0)
        # 安全拦截
        SAFETY_BLOCK.labels("injection").inc(0)
        # 告警回调（Grafana severity 维度）
        for _sev in ("info", "warning", "critical"):
            ALERT_RECEIVED.labels(_sev).inc(0)
        # 基础组件健康(先置 0，由后台周期任务刷为真实探活值)
        for _comp in ("mysql", "minio", "milvus", "redis"):
            COMPONENT_HEALTH.labels(_comp).set(0)
    except Exception:
        # 预注册失败不影响服务启动
        pass
