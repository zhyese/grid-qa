"""Prometheus 指标定义。"""
from threading import Lock

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
    "grid_llm_latency_seconds", "LLM 调用延迟(秒)", ["provider"],
    # 默认桶为亚秒级 HTTP 优化，LLM 调用普遍 1-10s 会全挤进 5.0/7.5 桶 → 分位估算粗。
    # 用适合 LLM 分布的桶（0.5s 快速/缓存命中 → 30s 慢调用，2-10s 粒度细）。
    buckets=(0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 7.5, 10.0, 15.0, 20.0, 30.0),
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
# HALLUC = LLM-judge 实测幻觉率（采样/离线/dislike 触发，真值）；UNGROUNDED_RATIO = 启发式未引用率（每次问答，廉价代理）
HALLUC = Histogram(
    "grid_hallucination_rate", "答案幻觉率分布(LLM-judge 实测,采样)",
    buckets=(0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, float("inf")),
)
UNGROUNDED_RATIO = Histogram(
    "grid_ungrounded_ratio", "未引用率(启发式:未被引用资料占比,每次问答的廉价代理)",
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
CRAG_CONFIDENCE = Counter("grid_crag_confidence_total", "答案置信度分布(high/medium/refused)", ["confidence"])
# 基础组件健康（MySQL/Milvus/Redis/MinIO 探活结果，1=up/0=down）—— 让 /health 状态进 Grafana 可监控可告警
COMPONENT_HEALTH = Gauge("grid_component_health", "基础组件健康状态(1=up/0=down)", ["component"])
# 安全合规：prompt injection 命中 + 答案脱敏次数（D4，电网强监管可见性）
SAFETY_BLOCK = Counter("grid_safety_block_total", "安全事件(prompt injection/敏感信息脱敏)", ["kind"])
# 电网危险操作关键词命中（按类别：接地安全/放电/带电作业/短路/误操作/...）
SAFETY_KEYWORD = Counter("grid_safety_keyword_total", "电网危险操作关键词命中", ["category"])
# 领域增强：故障诊断 / 两票生成 / 相似案例 调用次数（D1/D2/D3）
DOMAIN_CALLS = Counter("grid_domain_calls_total", "领域增强调用", ["feature"])
# 两票智能审核结果分布（pass/warn/fail）
TICKET_AUDIT = Counter("grid_ticket_audit_total", "两票智能审核结果", ["result"])
# Agentic 诊断：循环深度分布（观测 agent 调几轮工具）
AGENT_ITERS = Histogram("grid_agent_iters", "诊断 agent 循环深度(轮)",
                        buckets=(1, 2, 3, 4, 5, 6, float("inf")))
# 通用 Agent 引擎（S1）：persona 调用次数 + persona×工具 调用次数（为 S6 决策看板铺路）
AGENT_CALLS = Counter("grid_agent_calls_total", "Agent 引擎调用次数", ["persona"])
AGENT_TOOL_CALLS = Counter("grid_agent_tool_calls_total", "Agent 工具调用次数", ["persona", "tool"])
# S4：工具调用权限拒绝次数（高风险工具按 role 限）
AGENT_TOOL_DENIED = Counter("grid_agent_tool_calls_denied_total", "Agent 工具权限拒绝", ["tool"])
# 告警闭环：Grafana alerting webhook 回调接收到的告警数（按 severity）
ALERT_RECEIVED = Counter("grid_alert_received_total", "告警接收总数(Grafana回调)", ["severity"])
# 缓存分层命中（Redis / MySQL / LLM）—— Phase 2 三级缓存可见性
CACHE_HIT = Counter("grid_cache_hit_total", "缓存命中(分层)", ["layer"])
CACHE_MYSQL_FAIL = Counter("grid_cache_mysql_fail_total", "MySQL 缓存写入失败次数")
CACHE_MYSQL_ROWS = Gauge("grid_cache_mysql_rows", "qa_cache 表当前行数")
CACHE_EVICTED = Counter("grid_cache_evicted_total", "淘汰/清理行数", ["reason"])
# Query 改写评估（采纳/缓存命中/否决，按 strategy: rewrite/multi/hyde）
REWRITE_IMPROVED = Counter("grid_rewrite_improved_total", "改写被评估采纳次数", ["strategy"])
REWRITE_CACHE_HIT = Counter("grid_rewrite_cache_hit_total", "改写缓存命中次数", ["strategy"])
REWRITE_EVAL_REJECTED = Counter("grid_rewrite_eval_rejected_total", "改写被评估否决次数", ["strategy"])
# 智能路由（Phase A）：决策分布 + 延迟 + 路由偏差
ROUTING_DECISION = Counter("grid_routing_decision_total", "路由决策分布", ["route"])
ROUTING_LATENCY = Histogram("grid_routing_latency_seconds", "路由分类延迟(秒)")
ROUTING_MISMATCH = Counter("grid_routing_mismatch_total", "路由偏差(预期vs实际)", ["mismatch"])

# 检索调参扫描（只建议模式）：扫描次数 + baseline 指标趋势
RETRIEVAL_TUNE_TOTAL = Counter("grid_retrieval_tune_total", "检索调参扫描次数")
RETRIEVAL_BASELINE = Gauge("grid_retrieval_baseline", "检索 baseline 指标", ["metric"])

# ===== 数据飞轮度量（C3）=====
GOVERNANCE_PROPAGATED = Counter(
    "grid_governance_propagated_total",
    "治理联动清理次数(向量/图谱/缓存)",
    ["action"],
)
QUALITY_EVENT_TOTAL = Counter(
    "grid_quality_event_total",
    "质量事件总线吞吐与处理计数",
    ["source", "type"],
)
FEEDBACK_FIX_RATE = Gauge("grid_feedback_fix_rate", "坏case修复率(dislike→补全→同query再like)")
FAITHFULNESS_TREND = Gauge("grid_faithfulness_trend", "faithfulness 周环比")
KB_FRESHNESS = Gauge("grid_kb_freshness", "active文档占比(治理覆盖率)")

# ===== 进程内缓存命中 mirror =====
# 底层逻辑：prometheus_client Counter 进程内无法直接读值（只能抓 /metrics 文本），
# 而"优化建议"报告需要实时命中率做决策。这里维护一份进程内分层计数 mirror，
# 由 cache_hit_inc() 在每次 CACHE_HIT.labels(x).inc() 时同步自增，
# cache_hit_rate()/cache_hit_snapshot() 供优化建议报告读取。
_CACHE_HIT_INPROC: dict[str, int] = {}
_cache_lock = Lock()


def cache_hit_inc(layer: str) -> None:
    """缓存命中计数：同时写 prometheus（Grafana 用）+ 进程内 dict（优化建议报告用）。

    替代分散的 metrics.CACHE_HIT.labels(x).inc() 两段式写法，集中一处保证两路同步。
    """
    try:
        CACHE_HIT.labels(layer).inc()
    except Exception:
        pass
    with _cache_lock:
        _CACHE_HIT_INPROC[layer] = _CACHE_HIT_INPROC.get(layer, 0) + 1


def cache_hit_snapshot() -> dict:
    """返回进程内缓存命中分层快照（redis/mysql/semantic/llm）。"""
    with _cache_lock:
        return dict(_CACHE_HIT_INPROC)


def cache_hit_rate() -> float:
    """综合缓存命中率 = (redis+mysql+semantic) / (redis+mysql+semantic+llm)。

    无样本时返回 0.0（调用方需结合样本量判断是否采信）。
    """
    with _cache_lock:
        hit = (_CACHE_HIT_INPROC.get("redis", 0) + _CACHE_HIT_INPROC.get("mysql", 0)
               + _CACHE_HIT_INPROC.get("semantic", 0))
        total = hit + _CACHE_HIT_INPROC.get("llm", 0)
    return round(hit / total, 3) if total else 0.0


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
        # CRAG 答案置信度（证据有限/不足占比的一等指标）
        for _conf in ("high", "medium", "refused"):
            CRAG_CONFIDENCE.labels(_conf).inc(0)
        # 领域增强
        DOMAIN_CALLS.labels("diagnose").inc(0)
        DOMAIN_CALLS.labels("ticket").inc(0)
        DOMAIN_CALLS.labels("similar_case").inc(0)
        DOMAIN_CALLS.labels("safety_block").inc(0)
        DOMAIN_CALLS.labels("diagnose_agent").inc(0)
        DOMAIN_CALLS.labels("debate_diagnose").inc(0)
        # 两票审核结果（预注册 0 值，消除面板 No data 盲区）
        for _res in ("pass", "warn", "fail"):
            TICKET_AUDIT.labels(_res).inc(0)
        # 安全拦截
        SAFETY_BLOCK.labels("injection").inc(0)
        SAFETY_BLOCK.labels("deidentify").inc(0)
        # 电网危险操作关键词（8 类）
        for _hcat in ("接地安全", "放电", "带电作业", "短路", "误操作", "倒闸操作", "安全措施", "设备异常"):
            SAFETY_KEYWORD.labels(_hcat).inc(0)
        # 告警回调（Grafana severity 维度）
        for _sev in ("info", "warning", "critical"):
            ALERT_RECEIVED.labels(_sev).inc(0)
        # 基础组件健康(先置 0，由后台周期任务刷为真实探活值)
        for _comp in ("mysql", "minio", "milvus", "redis"):
            COMPONENT_HEALTH.labels(_comp).set(0)
        # 分层缓存命中（预注册 Redis/MySQL/LLM 三个 layer 序列）
        for _layer in ("redis", "mysql", "llm"):
            CACHE_HIT.labels(_layer).inc(0)
        # 缓存淘汰原因
        for _reason in ("lru", "ttl", "cleanup", "doc_update"):
            CACHE_EVICTED.labels(_reason).inc(0)
        # 改写评估（按 strategy 预注册 0 值，消除面板 No data 盲区）
        for _s in ("rewrite", "multi", "hyde"):
            REWRITE_IMPROVED.labels(_s).inc(0)
            REWRITE_CACHE_HIT.labels(_s).inc(0)
            REWRITE_EVAL_REJECTED.labels(_s).inc(0)
        # MySQL 缓存行数（初始 0）
        CACHE_MYSQL_ROWS.set(0)
        # 智能路由决策（预注册 4 个 route 序列）
        for _route in ("sparse", "dense", "hybrid", "sparse_first"):
            ROUTING_DECISION.labels(_route).inc(0)
        # Agent 引擎（S1）：diagnose persona + 其 4 工具预注册 0 值
        AGENT_CALLS.labels("diagnose").inc(0)
        for _agent_tool in ("search_regulation", "query_equipment_graph",
                            "search_similar_case", "draft_ticket"):
            AGENT_TOOL_CALLS.labels("diagnose", _agent_tool).inc(0)
        AGENT_TOOL_DENIED.labels("draft_ticket").inc(0)
        # 检索调参 baseline（recall/mrr/ndcg 预注册 0，扫描后更新）
        for _tune_m in ("recall", "mrr", "ndcg"):
            RETRIEVAL_BASELINE.labels(_tune_m).set(0)
        # 数据飞轮（C3）：治理联动清理 + 总线吞吐 预注册 0
        for _action in ("milvus", "neo4j", "qa_cache"):
            GOVERNANCE_PROPAGATED.labels(_action).inc(0)
        for _src in ("feedback", "online_eval", "qa_service", "retrieval_eval", "governance"):
            for _typ in ("dislike", "low_faith", "refused", "eval_low", "doc_blocked", "sampled"):
                QUALITY_EVENT_TOTAL.labels(_src, _typ).inc(0)
        FEEDBACK_FIX_RATE.set(0)
        FAITHFULNESS_TREND.set(0)
        KB_FRESHNESS.set(0)
    except Exception:
        # 预注册失败不影响服务启动
        pass
