"""全局配置：pydantic-settings 读取根目录 .env，导出 settings 单例。

预留 ConfigSource 抽象（EnvConfigSource 现实现 / NacosConfigSource 占位），
后续接 nacos 时只实现 NacosConfigSource，业务代码零改动。
"""
from functools import lru_cache

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
    REDIS_MAXMEMORY: str = "10mb"       # allkeys-lru 内存上限
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
    MMR_LAMBDA: float = 0.6          # 相关性 vs 多样性 权衡
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
    CRAG_HIGH: float = 0.6       # top1 rerank分>=此值=correct(证据充分)
    CRAG_LOW: float = 0.3        # top1 rerank分<此值=incorrect(触发纠错)
    CRAG_PERDOC_ENABLE: bool = False  # CRAG v2：LLM 逐条评估证据相关性（非仅 top1，增延迟，默认关）

    # ---------- 优化建议（反馈驱动）----------
    OPTIMIZER_CACHE_HIT_FLOOR: float = 0.20  # 缓存命中率低于此值才出缓存优化建议
    OPTIMIZER_MIN_SAMPLE: int = 10           # 缓存样本少于此值不采信命中率（不出建议）
    OPTIMIZER_TREND_RATIO: float = 1.2       # dislike 周环比≥此值预警"失分上升"

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
    ALERT_WEBHOOK_TOKEN: str = "grid-alert-token-2026"  # Grafana contact point 回调共享密钥（免 JWT）

    # ---------- 可信度评测（真 faithfulness，替代粗糙启发式）----------
    ONLINE_FAITHFULNESS_ENABLE: bool = True  # 线上答案异步 LLM-judge，前端拉取覆盖"幻觉率"展示
    FAITHFULNESS_GATE: float = 0.85          # 生成质量门禁：平均支撑率阈值（eval_generation 用）

    # ---------- 多模态 RAG（VLM 图片理解）----------
    VLM_ENABLE: bool = False       # VLM 理解图片(图纸/设备/故障现象)补充 OCR 丢失的空间语义
    QWEN_VLM_MODEL: str = "qwen-vl-max"

    # ---------- 配置中心（Nacos 可选覆盖，默认 .env）----------
    CONFIG_SOURCE: str = "env"        # env | nacos（nacos 时启动拉取覆盖 .env，降级安全）
    NACOS_SERVER: str = "http://localhost:8848"
    NACOS_NAMESPACE: str = ""         # 命名空间 id（留空=public）
    NACOS_GROUP: str = "DEFAULT_GROUP"
    NACOS_DATA_ID: str = "grid-qa.properties"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
