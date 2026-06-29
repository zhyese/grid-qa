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
    QA_CACHE_TTL: int = 3600

    # ---------- Neo4j（知识图谱：设备-故障-处置 多跳推理）----------
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "neo4j123456"

    # ---------- 重排 ----------
    RERANK_ENABLE: bool = True
    RERANK_MODEL: str = "gte-rerank-v2"

    # ---------- 检索质量 ----------
    MMR_ENABLE: bool = True          # MMR 多样性重排
    MMR_LAMBDA: float = 0.6          # 相关性 vs 多样性 权衡
    QUERY_REWRITE_ENABLE: bool = False  # LLM 改写 query（增延迟，默认关）
    BGE_MODEL: str = "BAAI/bge-small-zh-v1.5"   # 可换 bge-large-zh-v1.5(1024维,效果更好)
    BGE_DIM: int = 512
    DOC_SIZE_THRESHOLD: int = 5000   # 文档总字数超此值 → 走云 embedding；否则本地 bge
    MILVUS_COLLECTION_BGE: str = "grid_chunks_bge"   # 本地 bge 向量库(独立 collection, 向量空间不混)

    # ---------- 分块 ----------
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 80


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
