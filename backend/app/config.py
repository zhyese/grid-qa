"""全局配置：pydantic-settings 读取根目录 .env，导出 settings 单例。

预留 ConfigSource 抽象（EnvConfigSource 现实现 / NacosConfigSource 占位），
后续接 nacos 时只实现 NacosConfigSource，业务代码零改动。
"""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 应用 ----------
    APP_NAME: str = "电网运维 RAG 智能问答系统"
    APP_VERSION: str = "0.1.0"
    BACKEND_HOST: str = "127.0.0.1"
    BACKEND_PORT: int = 8001   # 本机 8000 被 Manager.exe 占用，固定 8001
    API_PREFIX: str = "/api"
    # 关=现状；开=独立运维工作台，不改变原 QA/缓存链路，不执行设备控制。
    OPS_WORKBENCH_ENABLE: bool = False
    DEBUG: bool = True
    STARTUP_DEPENDENCY_RETRIES: int = 0   # >0 时启动前等待 MySQL（单 Pod 演示模式）
    STARTUP_DEPENDENCY_INTERVAL: float = 2.0
    STARTUP_COMPONENT_RETRIES: int = 1    # >1 时 MinIO/Milvus 初始化失败会在启动期重试

    # ---------- MySQL ----------
    DATABASE_URL: str = (
        "mysql+aiomysql://grid:grid123456@localhost:3307/grid_qa?charset=utf8mb4"
    )
    MYSQL_HOST: str = "localhost"
    MYSQL_PORT: int = 3307
    MYSQL_USER: str = "grid"
    MYSQL_PASSWORD: str = "grid123456"
    MYSQL_DATABASE: str = "grid_qa"

    # ---------- MinIO ----------
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_BUCKET: str = "grid-documents"
    MINIO_SECURE: bool = False

    # ---------- JWT ----------
    JWT_SECRET: str = "please-change-this-to-a-random-long-string"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440

    # ---------- 默认管理员 ----------
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"

    # ---------- 模型 Provider ----------
    LLM_PROVIDER: str = "deepseek"      # deepseek | qwen | doubao
    EMB_PROVIDER: str = "qwen"          # qwen | doubao
    EMBEDDING_DIM: int = 1024
    EMB_BATCH_CONCURRENCY: int = 3      # 云 embedding 批次并发度（防 429，文档批量向量化加速）
    # B4：真实 token usage 透传（opt-in，默认关）。开关开 → qa_service 走 chat_with_usage
    # 拿 r.usage 真实 prompt/completion tokens 记录到 cost_tracker；关 → 沿用 len//2 估算。
    LLM_USAGE_TRACK_ENABLE: bool = False
    # LLM 生成性能（实测驱动）：qwen-plus 默认输出 1000+字致生成段 14-16s；砍 max_tokens + 简洁指令
    # 把单次总时长压到 ~8s。timeout/retry 防 deepseek 卡死/空 answer 拖到分钟级（OpenAI SDK 默认 600s/2次）。
    LLM_MAX_TOKENS: int = 768          # 单次生成上限（运维问答无需 2048 长篇）
    LLM_TIMEOUT: float = 30.0          # 单次请求超时（SDK 默认 600s 过大）
    LLM_MAX_RETRIES: int = 1           # 失败重试次数（默认 2 次指数退避，缩到 1 防尾部放大）
    # 模型路由（L0 fallback + L1 熔断 + L2 分档）：deepseek-v4-flash 实测返空 answer，
    # 原实现无 fallback 直接返空给用户。fallback 链按序切备；后台探活熔断故障 provider；
    # L2 分档 opt-in（运维问答多数规程查询，收益待 A/B 验证）。
    LLM_FALLBACK_CHAIN: str = "qwen,deepseek,doubao,ollama"   # fallback 链末位追加本地应急模型
    LLM_FALLBACK_ON_EMPTY: bool = True                 # 空 answer(如 deepseek 0字)也触发 fallback
    LLM_HEALTH_PROBE_ENABLE: bool = True               # 后台周期探活 provider（熔断底座）
    LLM_CIRCUIT_FAIL_N: int = 3                        # 连续失败 N 次 → 熔断冷却
    LLM_CIRCUIT_COOLDOWN: int = 60                     # 熔断冷却秒数
    LLM_PROBE_INTERVAL: int = 30                       # 健康探活周期秒
    LLM_TIER_ENABLE: bool = False                      # L2 query 特征分档(turbo/plus) opt-in 默认关
    RERANK_TIMEOUT: float = 2.0                        # rerank 单次超时（超时降级用 RRF 原序稳 p99；云 API 偶发 891ms+）
    LLM_LOCAL_TIMEOUT: float = 60.0                    # 本地 Ollama 兜底超时（CPU 推理慢于云端 API）

    # --- DeepSeek ---
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # --- 阿里百炼 DashScope ---
    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    QWEN_LLM_MODEL: str = "qwen-plus"
    QWEN_EMB_MODEL: str = "text-embedding-v3"

    # --- 火山方舟 Ark ---
    ARK_API_KEY: str = ""
    ARK_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/v3"
    DOUBAO_LLM_ENDPOINT_ID: str = ""
    DOUBAO_EMB_MODEL: str = "doubao-embedding-text-240815"

    # --- 本地 Ollama（云端 LLM 全部不可用时的应急兜底，L0 fallback 链末位）---
    OLLAMA_BASE_URL: str = "http://ollama:11434"
    OLLAMA_MODEL: str = "qwen2.5:7b-instruct-q4_K_M"

    # ---------- Milvus ----------
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    MILVUS_COLLECTION: str = "grid_chunks"

    # ---------- Redis（热点问答缓存）----------
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_MAXMEMORY: str = "300mb"      # allkeys-lru 内存上限
    QA_CACHE_TTL: int = 259200          # 3 天（72h），原 3600 命中率太低
    HOTQA_ENABLE: bool = True           # 复用点赞写入的高频问答对(hotqa:{nq}永久缓存，命中跳检索/CRAG/生成)
    # ---------- MySQL 二级缓存（Redis LRU 淘汰持久化）----------
    CACHE_PERSIST_ENABLE: bool = True     # Write-Through 双写 MySQL
    CACHE_PERSIST_CLEANUP_HOURS: int = 6  # 应用层清理周期（小时），兜底 MySQL Event Scheduler
    CACHE_WARMUP_ENABLE: bool = True       # 启动和周期预热开关（本地演示可关闭）
    LOG_ARCHIVE_ENABLE: bool = True        # 操作日志自动归档开关
    BACKUP_CRON_HOURS: float = 3.0         # <=0 关闭定时全量备份
    TASK_WORKERS_ENABLE: bool = True       # 持久化任务/事件后台 worker 开关
    CACHE_TIERED_TTL_ENABLE: bool = True  # 分层 TTL：手册 7d / 案例 3d / 实时 5min
    # ---------- B2/B3 缓存命中滑动续期（默认关，opt-in）----------
    CACHE_SLIDE_TTL_ENABLE: bool = False        # B2：L1 命中时 EXPIRE 续期，热 query 保活防 evict
    EMBED_CACHE_SLIDE_TTL_ENABLE: bool = False  # B3：embed_query 命中续期（高频 query 保活）
    EMBED_CACHE_TTL: int = 3600                  # B3：embedding 缓存 TTL（秒）
    # ---------- Batch 2 (A1/A4)：chunk 向量复用（默认关，opt-in）----------
    # chunk 内容入库后稳定，citation_verifier 校验2 / auto_cite / knowledge_evolution
    # 多处反复 embed 同一 chunk → 按 chunk_id 查 Redis 缓存，命中跳过，miss embed+存。
    EMBED_CHUNK_CACHE_ENABLE: bool = False        # 开关：默认关=现状；开才走 chunk_id 缓存
    EMBED_CHUNK_CACHE_TTL: int = 604800           # chunk 向量缓存 TTL（秒，默认 7 天）

    # ---------- Neo4j（知识图谱：设备-故障-处置 多跳推理）----------
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "neo4j123456"
    KG_RAG_ENABLE: bool = True   # 问答时融合知识图谱结构化上下文(GraphRAG)
    KG_AUTO_EXTRACT_ENABLE: bool = True  # 文档向量化后是否后台调用 LLM 自动抽取三元组
    KG_TOKENIZE_CACHE_ENABLE: bool = False  # B5：jieba 分词结果 Redis 缓存（默认关，opt-in）

    # ---------- 重排 ----------
    RERANK_ENABLE: bool = True
    RERANK_MODEL: str = "gte-rerank-v2"

    # ---------- 检索质量 ----------
    MMR_ENABLE: bool = True          # MMR 多样性重排
    MMR_LAMBDA: float = 0.5          # 相关性 vs 多样性 权衡（0.5 多样性更均衡）
    # ---------- RRF 融合（多路检索排名融合）----------
    RRF_K: int = 60                       # RRF 平滑常数（越小头部越集中）
    RRF_DENSE_WEIGHT: float = 1.0         # 稠密向量路权重
    RRF_SPARSE_WEIGHT: float = 1.0        # BM25 稀疏路权重（电网术语精确匹配可调高）
    # ---------- Batch 1 · routing-aware 调参总开关（A2/A3/A5/B6，默认关=现状）----------
    # 开 → mixed_search 按 routing_decision.route / features.query_type 动态调：
    #   A2 RRF 权重（dense/sparse_first 单路 ×1.3）/ A3 MMR λ（fault.7/mixed.5/natural.4）
    #   A5 rerank 早剪枝（topk*1.2 替代 topk*2 省额度）/ B6 Milvus ef（sparse_first 减半 / dense+fault 翻倍）
    # 关 → mixed_search 逐字节=现状（等权 RRF / 固定 λ / topk*2 / 固定 ef）
    RRF_ROUTE_AWARE_ENABLE: bool = False
    RAPTOR_ENABLE: bool = False      # RAPTOR 层次化摘要检索（多粒度融合，默认关）
    SEMANTIC_CACHE_ENABLE: bool = False  # 语义缓存（embedding相似度命中，默认关）
    QUERY_REWRITE_ENABLE: bool = False  # LLM 改写 query（增延迟，默认关）
    BGE_MODEL: str = "BAAI/bge-small-zh-v1.5"   # 可换 bge-large-zh-v1.5(1024维,效果更好)
    BGE_DIM: int = 512
    DOC_SIZE_THRESHOLD: int = 5000   # 文档总字数超此值 → 走云 embedding；否则本地 bge
    MILVUS_COLLECTION_BGE: str = "grid_chunks_bge"   # 本地 bge 向量库(独立 collection, 向量空间不混)

    # ---------- 分块 ----------
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 80

    # ---------- Corrective RAG（检索自纠错闭环）----------
    CRAG_ENABLE: bool = True     # 检索后分级+纠错(低相关触发query改写重检索/拒答)
    CRAG_NEIGHBOR_EXPAND_ENABLE: bool = False  # ambiguous 档邻域 chunk 扩展（补证据完整性，默认关）
    CRAG_NEIGHBOR_WINDOW: int = 1              # 邻域窗口（±N 个 chunk_idx）
    CRAG_HIGH: float = 0.72      # top1 rerank分>=此值=correct(证据充分; 原0.6太松致燃料少,0.72让更多medium进gap)
    CRAG_LOW: float = 0.3        # top1 rerank分<此值=incorrect(触发纠错)
    CRAG_PERDOC_ENABLE: bool = False  # CRAG v2：LLM 逐条评估证据相关性（非仅 top1，增延迟，默认关）
    CRAG_TIMEOUT: float = 5.0         # CRAG v2：LLM 评估单次超时限制（秒）
    CRAG_V3_ENABLE: bool = False  # confidence refinement 总开关: 连续置信度+归因+矩阵(关=现状3档,前端零改动)
    # ----- QA 主链路增强：接通旁支能力（opt-in，默认关=现状零破坏）-----
    DEBATE_ON_LOW_CONFIDENCE_ENABLE: bool = False  # 低置信(medium_low/low/refused)→debate_agent 三专家裁决共识注入答案+升置信
    SUFFICIENCY_GATE_ENABLE: bool = False           # judge.answerability 判证据不足→置信降一档(接通旁支 answerability)
    CONFIDENCE_OVERCONFIDENT_ENABLE: bool = True  # 断点G/B6: dislike×历史high置信冲突检测+evidence_gap复核(默认开)
    OVERCONFIDENT_BASELINE_TTL_DAYS: int = 30  # B6: highbase key 独立 TTL(不依赖问答缓存存活)

    # ---------- 检索参数调优（只建议模式）----------
    TUNE_ENABLE: bool = True
    TUNE_MIN_IMPROVE: float = 0.02      # 出建议的最小提升（防噪声）
    TUNE_MIN_SAMPLE: int = 10           # 最小有效样本（防小样本过拟合）
    TUNE_SCAN_TOPK: int = 5             # 扫描评测用 topk


    # ---------- 优化建议（反馈驱动）----------
    OPTIMIZER_CACHE_HIT_FLOOR: float = 0.20  # 缓存命中率低于此值才出缓存优化建议
    OPTIMIZER_MIN_SAMPLE: int = 10           # 缓存样本少于此值不采信命中率（不出建议）
    OPTIMIZER_TREND_RATIO: float = 1.2       # dislike 周环比≥此值预警"失分上升"
    OPTIMIZER_BLACKLIST_THRESHOLD: int = 2   # 同一 query dislike 累计≥此值自动进缓存黑名单

    # ---------- Query 改写升级（评估闭环+缓存+adaptive）----------
    REWRITE_CACHE_TTL: int = 604800            # 改写缓存 TTL（7 天）
    REWRITE_EVAL_ENABLE: bool = True           # 评估闭环开关（False=改写后不评估，盲用）
    REWRITE_ADAPTIVE_ENABLE: bool = True       # Classifier 判正常 query 时跳过改写（False=全部改写）
    REWRITE_EVAL_MARGIN: float = 0.05          # 评估更优阈值（new > orig*(1+margin)）
    REWRITE_EVAL_CAND: int = 10                # 评估检索候选数
    REWRITE_EVAL_TOPK: int = 5                 # 评估取 top-K 算分数和
    REWRITE_EVENT_SAMPLE_RATE: float = 1.0     # 改写事件采样率（高流量可降避免写放大）

    # ---------- 证据补全闭环 ----------
    EVIDENCE_GAP_AUTO_COLLECT: bool = True        # 自动收集 medium/refused
    EVIDENCE_GAP_DRAFT_TOPK_MULT: int = 2         # AI 续写检索放宽倍数
    EVIDENCE_GAP_FAQ_DOCTYPE: str = "证据补全FAQ"  # 同步入库的 docType
    # CRAG refused / stream 无结果 自动入证据补全队列（B4/B7）
    CRAG_REFUSED_TO_GAP_ENABLE: bool = True
    # ---------- C5 知识治理 fail-open 兜底（默认关=fail-closed 全拒，生产可开）----------
    KNOWLEDGE_GOVERNANCE_FAIL_OPEN: bool = False  # 治理存储异常时放行+DEGRADED告警(默认fail-closed)
    # ---------- C4 多轮 standalone 缓存扩面（默认关，opt-in）----------
    # 多轮指代消解后(search_q!=nq)也缓存：key=search_q+conv_id 隔离对话,_cache_knowledge_valid 复核文档时效兜底脏命中。
    MULTI_TURN_CACHE_ENABLE: bool = False

    # ---------- 结构感知分块 + Parent-Child（small-to-big）----------
    # 检索用小块（精度），命中后召回同组大块给 LLM（完整上下文，解决长规程跨块/表格被切两半）
    SMALL_TO_BIG_ENABLE: bool = True
    PARENT_SIZE: int = 2000      # 父块（大块，生成上下文）目标字数
    PARENT_OVERLAP: int = 200    # 子块尺寸复用 CHUNK_SIZE/CHUNK_OVERLAP

    # ---------- 检索增强（2026 RAG 趋势，默认关，按需开）----------
    HYDE_ENABLE: bool = False        # HyDE：LLM 生成假设答案再做向量检索（短/口语问题提升召回）
    MULTI_QUERY_ENABLE: bool = False # 多查询分解：复杂问题拆子问题并行检索
    SELF_RAG_ENABLE: bool = False    # Self-RAG：LLM 判断是否需检索/证据是否足够
    STANDALONE_REWRITE_ENABLE: bool = True  # 多轮指代消解：把追问改写成带上下文的独立查询
    ROUTING_ENABLE: bool = True      # ★ 智能路由：查询特征→自动选择检索路径(sparse/dense/hybrid)

    # ---------- 安全合规（电网强监管）----------
    SAFETY_FILTER_ENABLE: bool = True   # 入站 prompt injection 防护
    INJECTION_GUARD_STRICT_ENABLE: bool = False  # 红队缺口#2: 高危注入(指令覆盖类)QA侧直接结构化拒答(关=现状只告警防误杀)
    PII_MASK_ENABLE: bool = False       # 出站答案敏感信息脱敏（默认关，按合规要求开）
    HIGH_RISK_KEYWORDS: str = "停电,拉闸,合闸,接地,挂地线,带电,登高,攀登,放电,倒闸"

    # ---------- 告警闭环（Grafana alerting → webhook 落库进日志页）----------
    ALERT_WEBHOOK_TOKEN: str = ""  # Grafana contact point 回调共享密钥（免 JWT）；未配置时拒绝接入
    ALERT_WEBHOOK_TENANT: str = "default"     # Grafana webhook 固定写入租户，禁止由请求头任意指定
    # 实时接入凭据必须显式配置，绝不复用源码内置的 Grafana webhook token。
    # 单凭据只授权 REALTIME_EVENT_CREDENTIAL_TENANT；多租户连接器使用 JSON 映射。
    REALTIME_EVENT_CREDENTIAL_TENANT: str = "default"
    REALTIME_EVENT_TOKEN: str = ""
    REALTIME_EVENT_SIGNING_SECRET: str = ""
    REALTIME_EVENT_TENANT_TOKENS: dict[str, str] = Field(default_factory=dict)
    REALTIME_EVENT_TENANT_SIGNING_SECRETS: dict[str, str] = Field(default_factory=dict)

    # ---------- 可信度评测（真 faithfulness，替代粗糙启发式）----------
    ONLINE_FAITHFULNESS_ENABLE: bool = True  # 线上答案异步 LLM-judge，前端拉取覆盖"幻觉率"展示
    FAITHFULNESS_GATE: float = 0.85          # 生成质量门禁：平均支撑率阈值（eval_generation 用）

    # ---------- 证据溯源（P4-⑮ 句级角标）----------
    CITATION_AUTO_ENABLE: bool = True       # 无角标句子是否向量相似度自动补标
    CITATION_SIM_THRESHOLD: float = 0.6     # 自动补标 cosine 阈值（低于则不补，保留"无引用"）
    # ===== 可核验引用引擎（五层闭环，全 opt-in，默认=现状）=====
    CITATION_VERIFIER_ENABLE: bool = False   # 第四层校验引擎总开关（格式+向量+NLI）
    CITATION_NLI_ENABLE: bool = False        # 校验3 NLI 精准核验（最重，独立开关）
    CITATION_NLI_TIMEOUT: int = 5            # 校验3 NLI 超时秒（超时降级仅走校验1+2）
    CITATION_NLI_ASYNC_ENABLE: bool = False  # C1 校验3 NLI 异步后置（done 后后台跑,不阻塞首答;关=verify 内同步现状）

    # ---------- 数据飞轮修复率（B1）----------
    FIX_RATE_ENABLE: bool = True        # 修复率聚合开关（cron 调 recompute_fix_rate）
    FIX_RATE_WINDOW_DAYS: int = 30      # 统计窗口（近 N 天的 dislike 计入分母）
    FIX_RATE_CRON_MINUTES: int = 30     # 周期 cron 调度间隔（分钟）—— Task 2 接入

    # ---------- 知识自进化草稿回流回测（B5）----------
    EVOLUTION_RETEST_ENABLE: bool = True   # 回测开关：run_scan 入口对 indexed 草稿重跑 member_queries 算 lift
    EVOLUTION_RETEST_AFTER_DAYS: int = 7   # 回测触发阈值：仅 indexed_at 早于 N 天的草稿参与回测（给回流留生效窗口）
    KNOWLEDGE_EVOLUTION_CRON_HOURS: float = 24.0  # <=0 关闭自动扫描
    EVIDENCE_GAP_DEEP_INTERVAL: float = 180.0     # <=0 关闭自动深度补全
    EVIDENCE_GAP_DEEP_BATCH: int = 5

    # ---------- judge 聚合差评文档 → 治理 issue（B3 路径3，不动在线权重）----------
    DOC_QUALITY_ISSUE_ENABLE: bool = True        # 开关：governance scan 末尾聚合差评文档生成 quality_low issue
    DOC_QUALITY_DISLIKE_THRESHOLD: float = 0.5   # dislike 率阈值：≥ 此值触发（默认 50%）
    DOC_QUALITY_MIN_COUNT: int = 3               # 最小 dislike 样本数：低于此数不触发（防小样本误判）
    DOC_QUALITY_WINDOW_DAYS: int = 30            # 统计窗口（近 N 天的 Feedback 计入聚合）

    # ===== 数据飞轮（跨闭环质量事件总线，opt-in，默认=现状）=====
    QUALITY_BUS_ENABLE: bool = False  # 质量事件总线总开关（emit 入库恒做；开=异步派发订阅者）
    DISLIKE_TO_GAP_ENABLE: bool = False  # B1 dislike→质量事件(→evidence_gap 补全)；opt-in
    GOVERNANCE_PROPAGATE_ENABLE: bool = False  # A3/A4 治理联动清理 Milvus/Neo4j/qa_cache；opt-in
    GOVERNANCE_PROPAGATE_DRY_RUN_ENABLE: bool = False  # A4 安全阀: 开=只产出候选清理报告(质量事件)不删除，人工确认后走 execute 端点；关=事件直接触发真实清理
    SEMANTIC_CACHE_GOV_FILTER_ENABLE: bool = False  # A5 semantic 命中后过 blocked_document_ids 过滤；opt-in
    EVAL_EMIT_ENABLE: bool = False  # B3 评测低分 emit(online_eval.low_faith/retrieval_eval.eval_low)；opt-in
    EVAL_TO_TUNE_ENABLE: bool = False  # C1 评测低分→retrieval_tune 订阅触发扫描；opt-in
    GOVERNANCE_UPLOAD_REQUIRE: bool = False  # C2 上传引导治理元数据（建 KnowledgeDocumentMetadata）；opt-in
    CITATION_STRUCTURED_OUTPUT: bool = False  # 第三层 LLM 结构化输出 CitationAnswer
    CITATION_REWRITE_ON_FAIL: bool = True    # 校验失败联动 CRAG：rewrite 二次检索 / refused 拒答
    CITATION_VERIFY_SIM_THRESHOLD: float = 0.4  # 校验2专用阈值(答案综合句vs原文chunk),独立于auto_cite补标CITATION_SIM_THRESHOLD=0.6(答案句LLM重组与原文cosine偏低,0.6误杀)

    # ---------- 多模态 RAG（VLM 图片理解）----------
    VLM_ENABLE: bool = False       # VLM 理解图片(图纸/设备/故障现象)补充 OCR 丢失的空间语义
    QWEN_VLM_MODEL: str = "qwen-vl-max"

    # ---------- 配置中心（Nacos 可选覆盖，默认 .env）----------
    CONFIG_SOURCE: str = "env"        # env | nacos（nacos 时启动拉取覆盖 .env，降级安全）
    NACOS_SERVER: str = "http://localhost:8848"
    NACOS_NAMESPACE: str = ""         # 命名空间 id（留空=public）
    NACOS_GROUP: str = "DEFAULT_GROUP"
    NACOS_DATA_ID: str = "grid-qa.properties"

    # ---------- N4 LLM 全链路可观测性 ----------
    OTEL_SAMPLE_RATE: float = 1.0          # 采样率：开发期 1.0(100%)，上线后 0.1(10%) + 异常必采
    OTEL_ENDPOINT: str = "http://localhost:3001/api/public/otel"  # Langfuse OTLP HTTP 端点
    OTEL_SERVICE_NAME: str = "grid-qa-backend"  # OTel service.name 标识
    # langfuse OTLP 鉴权（Basic Auth，双双非空才带头；scripts/langfuse_bootstrap.py 或 UI 创建）
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""  # 标准 OTLP collector 基地址；LLM User 探针优先使用
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: str = ""  # 显式 traces 接口，例 http://collector:4318/v1/traces
    LLM_USER_OBSERVER_ENABLED: bool = False
    LLM_USER_OBSERVER_USER_HASH_SECRET: str = ""
    LLM_USER_OBSERVER_QUEUE_SIZE: int = 2048
    LLM_USER_OBSERVER_BATCH_SIZE: int = 256
    LLM_USER_OBSERVER_CAPTURE_TEXT: bool = False  # 显式开启后也只采集规则脱敏摘要
    LLM_USER_CALLBACK_SECRET: str = ""          # 评测服务回调 HMAC 密钥，未配置时拒绝入口
    LLM_USER_CALLBACK_MAX_SKEW_SECONDS: int = 300
    LLM_USER_SUITE_EVENT_URL: str = ""          # 测试实例草稿回流后通知评测套件复测
    LLM_USER_SUITE_EVENT_SECRET: str = ""       # 与套件 LLM_USER_GRID_EVENT_SECRET 对应
    # ---------- 链路 trace 可视化（per-request 瀑布图，定位"卡在哪个节点"） ----------
    QA_TRACE_ENABLE: bool = True           # 总开关：采集各阶段耗时随响应返回前端 + 落库
    QA_TRACE_SAMPLE_RATE: float = 1.0      # 落库采样率（响应体 trace 不采样，实时展示必带）
    QA_TRACE_DETAIL_ENABLE: bool = False   # 节点详情采集(参数/prompt attrs, 纯观测不进缓存版本; 关=现状只记耗时)
    QA_TRACE_PROMPT_CHARS: int = 1200      # 单段 prompt/输出截断长度（attrs 大小预算的前置）
    # FAITHFULNESS_GATE 已在上方"可信度评测"区定义(0.85)，复用，不重复声明

    # ---------- N1 Agent 长期记忆层 ----------
    MEMORY_CAPACITY: int = 500             # 单用户记忆容量上限（条）
    MEMORY_DECAY_90D: float = 0.5          # 90 天未命中 weight × 此值
    MEMORY_DECAY_180D: float = 0.2         # 180 天未命中 weight × 此值
    MEMORY_SOFT_DELETE_DAYS: int = 30      # 软删除审计保留天数（过期物理删除）
    MEMORY_EXTRACT_MIN_TURNS: int = 3      # 工具调用型长对话累积 ≥N 轮才触发抽取
    MEMORY_COLLECTION: str = "memory_collection"  # Milvus 记忆 collection 名
    MEMORY_ENABLE: bool = True              # Agent 长期记忆 collection/检索总开关
    MEMORY_DECAY_CRON_HOURS: float = 24.0  # 记忆衰减+软删物理删除 周期(小时，<=0 关闭)；decay() 已实现但需 cron 触发
    MEMORY_AUTO_SAVE_ENABLED: bool = False # 全局自动沉淀开关（关=仅用户显式 opt-in 才写长期记忆）

    # ---------- N2b MCP 治理 ----------
    MCP_EXTERNAL_ENABLED: bool = True      # MCP 外部工具总开关（关=所有外部 server 不启用）
    MCP_PROVIDER_ALLOWLIST: str = ""       # server 名白名单（逗号分隔，空=全部已配置的允许）
    MCP_CONNECT_TIMEOUT_SECONDS: float = 2.0   # 连接超时（秒）
    MCP_DISCOVERY_TIMEOUT_SECONDS: float = 10.0  # 工具发现读超时（秒）
    MCP_CALL_TIMEOUT_SECONDS: float = 30.0      # 工具调用读超时（秒）

    # ---------- N2 MCP 工具总线 ----------
    MCP_SERVERS: str = ""                  # JSON 配置：[{"name":"mock_scada","url":"http://localhost:9100","token":"xxx"}]
    MCP_TOKEN: str = "grid-mcp-token-2026" # MCP server 对外暴露的鉴权 token
    MCP_SERVER_HOST: str = "0.0.0.0"       # MCP server 监听地址
    MCP_SERVER_PORT: int = 9100            # MCP server 监听端口
    MCP_IP_WHITELIST: str = ""             # IP 白名单（逗号分隔，空=不限）

    # ---------- N3 数字孪生变电站 3D ----------
    TWIN_LAYOUT_PATH: str = "app/data/station_layout_110kv.json"  # 110kV 站布局模板路径

    # ---------- 问答→行动闭环（工单工作流打通）----------
    # 问答→行动闭环：Agent 工具 create_ticket/submit_ticket + ops_planner persona + 工单流转事件（关=现状/开=工单闭环打通）
    TICKET_ACTION_LOOP_ENABLE: bool = False

    # ---------- 主动运维闭环补全（结构化根因 + 遥测证据 + 闭环回填，全部默认关=现状）----------
    # 结构化根因：开=proactive_diagnosis persona 输出 schema v2（根因列表/证据/置信度）；关=现状用 alert persona 自由 JSON
    PROACTIVE_SCHEMA_V2_ENABLE: bool = False
    # 遥测证据：开=诊断工具集并入 query_telemetry（mock_scada MCP 注册名），prompt 注入设备上下文；关=现状三只读工具
    PROACTIVE_TELEMETRY_ENABLE: bool = False
    # 闭环回填：confirm/reject/to-ticket 成功后 emit 质量事件（source=proactive-ops）；关=现状不 emit
    PROACTIVE_FEEDBACK_ENABLE: bool = False

    # ---------- 可视化工作流编排（DAG，BRD §5.3.1）----------
    # 可视化工作流编排：开=/api/workflows 全套 API（拖拽 DAG→保存→异步执行→运行历史）；关=现状无此功能
    WORKFLOW_ENABLE: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


_gov_generation: int = 0  # 治理代际进程内存镜像；bump_gov_generation_inproc() 同步自增


def bump_gov_generation_inproc() -> None:
    """治理代际 +1（进程内存）。governance_propagate_service 同时 Redis INCR 持久化。

    多进程部署需 Redis pub/sub 同步——当前单进程后端进程内存即可。
    """
    global _gov_generation
    _gov_generation += 1


def citation_cache_version() -> str:
    """citation 开关版本串 + 治理代际 G，拼进缓存 key/hash；开关变/G 变→key 变→旧缓存自动失效。

    避免 rebuild + 改开关后旧 query 命中改前行为的缓存（citation_extras 进了缓存）；
    A5 数据飞轮加 G 段：治理态变（withdraw/supersede/expire）→ governance_propagate_service
    bump Redis qa:gov_gen + 进程内存镜像 → G 变 → 所有 qa 缓存 key 变 → 自动失效。
    """
    s = settings
    return (f"cv{int(s.CITATION_VERIFIER_ENABLE)}{int(s.CITATION_STRUCTURED_OUTPUT)}"
            f"{int(s.CITATION_NLI_ENABLE)}{_gov_generation}"
            f"R{int(getattr(s, 'CRAG_V3_ENABLE', False))}"
            f"S{int(getattr(s, 'INJECTION_GUARD_STRICT_ENABLE', False))}")  # S: 注入拦截档(红队#2)/QA persona 防线语义代际
