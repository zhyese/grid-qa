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
    DEBUG: bool = True

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

    # ---------- Milvus ----------
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    MILVUS_COLLECTION: str = "grid_chunks"

    # ---------- Redis（热点问答缓存）----------
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_MAXMEMORY: str = "300mb"      # allkeys-lru 内存上限
    QA_CACHE_TTL: int = 259200          # 3 天（72h），原 3600 命中率太低
    # ---------- MySQL 二级缓存（Redis LRU 淘汰持久化）----------
    CACHE_PERSIST_ENABLE: bool = True     # Write-Through 双写 MySQL
    CACHE_PERSIST_CLEANUP_HOURS: int = 6  # 应用层清理周期（小时），兜底 MySQL Event Scheduler
    CACHE_TIERED_TTL_ENABLE: bool = True  # 分层 TTL：手册 7d / 案例 3d / 实时 5min

    # ---------- Neo4j（知识图谱：设备-故障-处置 多跳推理）----------
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "neo4j123456"
    KG_RAG_ENABLE: bool = True   # 问答时融合知识图谱结构化上下文(GraphRAG)

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
    CRAG_HIGH: float = 0.6       # top1 rerank分>=此值=correct(证据充分)
    CRAG_LOW: float = 0.3        # top1 rerank分<此值=incorrect(触发纠错)
    CRAG_PERDOC_ENABLE: bool = False  # CRAG v2：LLM 逐条评估证据相关性（非仅 top1，增延迟，默认关）
    CRAG_TIMEOUT: float = 5.0         # CRAG v2：LLM 评估单次超时限制（秒）

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
    # FAITHFULNESS_GATE 已在上方"可信度评测"区定义(0.85)，复用，不重复声明

    # ---------- N1 Agent 长期记忆层 ----------
    MEMORY_CAPACITY: int = 500             # 单用户记忆容量上限（条）
    MEMORY_DECAY_90D: float = 0.5          # 90 天未命中 weight × 此值
    MEMORY_DECAY_180D: float = 0.2         # 180 天未命中 weight × 此值
    MEMORY_SOFT_DELETE_DAYS: int = 30      # 软删除审计保留天数（过期物理删除）
    MEMORY_EXTRACT_MIN_TURNS: int = 3      # 工具调用型长对话累积 ≥N 轮才触发抽取
    MEMORY_COLLECTION: str = "memory_collection"  # Milvus 记忆 collection 名

    # ---------- N2 MCP 工具总线 ----------
    MCP_SERVERS: str = ""                  # JSON 配置：[{"name":"mock_scada","url":"http://localhost:9100","token":"xxx"}]
    MCP_TOKEN: str = "grid-mcp-token-2026" # MCP server 对外暴露的鉴权 token
    MCP_SERVER_HOST: str = "0.0.0.0"       # MCP server 监听地址
    MCP_SERVER_PORT: int = 9100            # MCP server 监听端口
    MCP_IP_WHITELIST: str = ""             # IP 白名单（逗号分隔，空=不限）

    # ---------- N3 数字孪生变电站 3D ----------
    TWIN_LAYOUT_PATH: str = "app/data/station_layout_110kv.json"  # 110kV 站布局模板路径


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
