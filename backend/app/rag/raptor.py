"""RAPTOR 风格层次化摘要检索（Recursive Abstractive Processing）。

核心思路：文档分块 → 聚类 → 聚类摘要 → 递归构建摘要树 → 多粒度检索。
简化实现：两层树（文档摘要 + 段落摘要 + 原文chunk），不做全量聚类。

检索策略：query 同时匹配原文 chunk + 段落摘要 + 文档摘要，
用 RRF 融合排序后返回，让相关摘要段也作为 LLM 上下文给到生成器，
提升长文档/跨文档问答的 recall。
"""
import asyncio
import json
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.obs import degraded
from app.models.chunk import Chunk
from app.models.document import Document
from app.providers.factory import get_llm_provider
from app.routing.routing_service import RouteDecision
from app.services import embedding_service
from app.services.term_service import normalize

_SUMMARY_TTL = 86400 * 30  # 30天重新生成摘要

# 摘要 prompt（压缩成一段概述）
_SUMMARIZE_PROMPT = """你是电网运维文档摘要专家。请基于以下文本生成一段摘要，覆盖关键设备、规程要求、操作步骤和注意事项。
要求：
1) 长度 100-200 字
2) 保留设备型号、参数限值、标准操作步骤等关键信息
3) 去掉示例、重复说明和格式占位符
4) 如果原文包含表格数据，保留核心数值和对比
5) 如果原文是故障案例，保留故障现象、原因和处置方式

文本：
{text}"""

_LEVEL_LABELS = {0: "原文chunk", 1: "段落摘要", 2: "文档摘要"}


class RaptorSummary:
    """单条摘要节点。"""
    def __init__(self, level: int, doc_id: str, doc_name: str, section: str,
                 chunk_indices: list[int], summary_text: str):
        self.level = level          # 0=原文, 1=段落摘要, 2=文档摘要
        self.doc_id = doc_id
        self.doc_name = doc_name
        self.section = section or ""
        self.chunk_indices = chunk_indices  # 此摘要覆盖的子 chunk 索引
        self.summary_text = summary_text
        self.embedding: list[float] | None = None


# ---------- 摘要生成 ----------

async def generate_chunk_summary(db: AsyncSession, doc_id: str, chunks: list[dict],
                                  model_type: str | None = None) -> list[RaptorSummary]:
    """为文档的每个段落组生成一层摘要，返回摘要节点列表。

    按 section 分组 → 每组生成一个段落摘要 → level=1。
    整个文档生成一个文档摘要 → level=2。
    """
    provider = get_llm_provider(model_type)
    summaries: list[RaptorSummary] = []

    if not chunks:
        return summaries

    doc_name = next((c.get("docName", "") for c in chunks if c.get("docName")), doc_id)

    # 按 section 分组
    sections: dict[str, list[tuple[int, str]]] = {}
    for i, c in enumerate(chunks):
        sec = c.get("section", "正文") or "正文"
        text = c.get("chunk", c.get("text", "")) or ""
        if sec not in sections:
            sections[sec] = []
        sections[sec].append((i, text))

    # 每个 section 生成摘要
    for sec_name, items in sections.items():
        texts = [t for _, t in items]
        combined = "\n\n".join(texts)
        if len(combined) < 100:
            # 太短不需要摘
            continue
        try:
            summary = await provider.chat(
                [{"role": "user", "content": _SUMMARIZE_PROMPT.format(text=combined[:4000])}],
                temperature=0.2, max_tokens=500,
            )
            if summary and len(summary.strip()) > 20:
                summaries.append(RaptorSummary(
                    level=1, doc_id=doc_id, doc_name=doc_name,
                    section=sec_name,
                    chunk_indices=[idx for idx, _ in items],
                    summary_text=summary.strip(),
                ))
        except Exception as e:
            degraded(f"raptor_summary_{sec_name}", e)

    # 文档摘要（所有 sections 合并后摘要）
    all_text = "\n\n".join(c.get("chunk", c.get("text", "")) or "" for c in chunks)
    if len(all_text) > 200:
        try:
            doc_summary = await provider.chat(
                [{"role": "user", "content": _SUMMARIZE_PROMPT.format(text=all_text[:6000])}],
                temperature=0.2, max_tokens=500,
            )
            if doc_summary and len(doc_summary.strip()) > 20:
                summaries.append(RaptorSummary(
                    level=2, doc_id=doc_id, doc_name=doc_name,
                    section="全文摘要",
                    chunk_indices=list(range(len(chunks))),
                    summary_text=doc_summary.strip(),
                ))
        except Exception as e:
            degraded("raptor_doc_summary", e)

    # 异步生成 embedding
    try:
        texts_to_embed = [s.summary_text for s in summaries]
        if texts_to_embed:
            embeds = await embedding_service.get_embeddings(texts_to_embed)
            for s, emb in zip(summaries, embeds):
                s.embedding = emb
    except Exception as e:
        degraded("raptor_embed", e)

    return summaries


# ---------- 存储 & 检索 ----------

_RAPTOR_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "raptor_cache"


def _cache_key(doc_id: str) -> str:
    return f"raptor_{doc_id}.json"


def save_summaries(doc_id: str, summaries: list[RaptorSummary]) -> None:
    """将摘要序列化缓存到本地文件（避免每次重启重建）。"""
    _RAPTOR_CACHE_PATH.mkdir(parents=True, exist_ok=True)
    data = [
        {"level": s.level, "docId": s.doc_id, "docName": s.doc_name,
         "section": s.section, "chunkIndices": s.chunk_indices,
         "summary": s.summary_text, "embedding": s.embedding}
        for s in summaries
    ]
    try:
        (_RAPTOR_CACHE_PATH / _cache_key(doc_id)).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        degraded("raptor_cache_save", e)


def load_summaries(doc_id: str) -> list[dict]:
    """从缓存加载摘要。"""
    p = _RAPTOR_CACHE_PATH / _cache_key(doc_id)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


async def retrieve_with_raptor(
    db: AsyncSession, query: str, topk: int = 5,
    tenant: str = "default", routing_decision: RouteDecision | None = None,
) -> list[dict]:
    """多粒度检索：原文chunk + 段落摘要 + 文档摘要 → RRF 融合。

    对摘要层做语义检索（cosine），与原有 chunk 检索 RRF 融合。
    返回格式兼容 mixed_search。
    """
    t0 = time.time()

    # 1) 收集所有文档的摘要（从缓存或实时生成）
    result = await db.execute(
        select(Document.id, Document.doc_name).where(
            Document.status == "vectorized", Document.tenant_id == tenant
        )
    )
    docs = result.all()
    all_summaries: list[dict] = []
    for doc_id, doc_name in docs:
        cached = load_summaries(doc_id)
        if cached:
            all_summaries.extend(cached)
        # 无缓存时不自动生成（触发在文档向量化后由 parse_service 调用）

    if not all_summaries:
        return []  # 无摘要可用，回退纯 chunk 检索

    # 2) query embedding
    try:
        q_emb = (await embedding_service.get_embeddings([query]))[0]
    except Exception as e:
        degraded("raptor_query_embed", e)
        return []

    # 3) 对摘要层做 cosine 相似度搜索
    import numpy as np
    q_np = np.array(q_emb, dtype=np.float32)
    scored: list[dict] = []
    for s in all_summaries:
        emb = s.get("embedding")
        if not emb:
            continue
        s_np = np.array(emb, dtype=np.float32)
        cos = float(np.dot(q_np, s_np) / (np.linalg.norm(q_np) * np.linalg.norm(s_np) + 1e-10))
        if cos > 0.3:  # 低阈值，RRF 会做归一化
            level = s.get("level", 0)
            scored.append({
                "docId": s.get("docId", ""),
                "docName": s.get("docName", ""),
                "chunk": s.get("summary", ""),
                "section": s.get("section", ""),
                "score": cos,
                "source": f"raptor_l{level}",
                "level": level,
                "chunkIndices": s.get("chunkIndices", []),
            })

    # 排序取 topk
    scored.sort(key=lambda x: -x["score"])
    return scored[:topk]


async def generate_and_cache_summaries(
    db: AsyncSession, doc_id: str, model_type: str | None = None,
) -> int:
    """文档向量化后触发：读取 chunk → 生成摘要 → 缓存。

    在 document_service 向量化完成后调用。
    """
    # 读 chunk
    result = await db.execute(
        select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_idx)
    )
    chunks = result.scalars().all()
    if not chunks:
        return 0

    chunk_dicts = [
        {"docName": c.doc_name, "section": c.section or "", "chunk": c.content, "text": c.content}
        for c in chunks
    ]
    summaries = await generate_chunk_summary(db, doc_id, chunk_dicts, model_type)
    save_summaries(doc_id, summaries)
    return len(summaries)