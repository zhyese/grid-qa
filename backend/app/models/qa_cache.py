"""问答缓存模型（Redis LRU 淘汰 → MySQL 冷备层）。

查询路径: Redis(L1) → MySQL(L2) → LLM(L3)
"""
import hashlib
from datetime import datetime, timedelta

from sqlalchemy import DateTime, Float, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QaCache(Base):
    __tablename__ = "qa_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default", index=True)
    cache_key: Mapped[str] = mapped_column(String(512), nullable=False, comment="缓存键: qa:{tenant}:{model}:{normalized}")
    model_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    query_hash: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, index=True,
        comment="MD5(cache_key) 精确匹配",
    )
    query_normalized: Mapped[str] = mapped_column(Text, nullable=False, comment="归一化后的问题")
    query_original: Mapped[str] = mapped_column(Text, nullable=False, comment="用户原始问题")
    answer: Mapped[str] = mapped_column(Text, nullable=False, comment="问答结果 JSON（与 Redis 缓存同结构）")
    retrieval_sources: Mapped[dict | None] = mapped_column(JSON, default=None, comment="引用来源")
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="high")
    hallucination_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="累计命中次数")
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=259200)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="过期时间")
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime, default=None, comment="最后一次命中时间")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(),
    )
    is_deleted: Mapped[bool] = mapped_column(Integer, default=0, comment="软删标记")

    @staticmethod
    def build_hash(model_type: str | None, normalized_query: str, tenant_id: str = "default") -> str:
        """MD5 哈希：cache_key → 32 位 hex，用于 MySQL 精确匹配。"""
        from app.config import citation_cache_version
        raw = f"qa:{tenant_id or 'default'}:{model_type or 'default'}:{normalized_query}:{citation_cache_version()}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def compute_expires(ttl_seconds: int = 259200) -> datetime:
        """计算过期时间 = now + ttl。"""
        return datetime.utcnow() + timedelta(seconds=ttl_seconds)

    @staticmethod
    def ttl_for_query(query: str) -> int:
        """分层 TTL：根据问题类型返回不同过期时间（Phase 3 智能优化）。

        运维手册/操作规程 → 7 天（604800s）
        故障案例/诊断      → 3 天（259200s）默认
        时效性/实时数据    → 5 分钟（300s）
        """
        short_keywords = ["实时", "当前", "现在", "最新", "今日", "今日故障", "当前状态"]
        long_keywords = ["步骤", "规程", "操作", "标准", "规范", "手册", "导则", "规定",
                         "要求", "流程", "程序", "方法"]
        q_lower = query.lower()
        for kw in short_keywords:
            if kw in q_lower:
                return 300
        for kw in long_keywords:
            if kw in q_lower:
                return 604800
        return 259200  # 默认 3 天
