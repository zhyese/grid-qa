"""BM25 稀疏检索（rank-bm25 + jieba 分词）。内存维护全量 chunk 语料。

Milvus 2.4 用外部 BM25 + RRF 实现混合检索（避免依赖 Milvus 内置全文检索版本约束）。
"""
from typing import Optional

import jieba
from rank_bm25 import BM25Okapi
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.document import Document

_corpus: list[dict] = []          # [{text, doc_id, doc_name, chunk_idx}]
_tokenized: list[list[str]] = []
_bm25: Optional[BM25Okapi] = None


def _tokenize(text: str) -> list[str]:
    return [w for w in jieba.cut(text or "") if w.strip()]


async def rebuild(db: AsyncSession) -> int:
    global _corpus, _tokenized, _bm25
    rows = (
        await db.execute(
            select(Chunk, Document)
            .join(Document, Chunk.doc_id == Document.id)
            .order_by(Chunk.doc_id, Chunk.chunk_idx)
        )
    ).all()
    _corpus = [
        {"text": c.content, "doc_id": c.doc_id, "doc_name": d.doc_name, "chunk_idx": c.chunk_idx}
        for c, d in rows
    ]
    _tokenized = [_tokenize(c.content) for c, _ in rows]
    _bm25 = BM25Okapi(_tokenized) if _tokenized else None
    return len(_corpus)


async def ensure_built(db: AsyncSession) -> None:
    if _bm25 is None:
        await rebuild(db)


def search(query: str, topk: int = 20) -> list[dict]:
    if not _bm25:
        return []
    scores = _bm25.get_scores(_tokenize(query))
    ranked = sorted(enumerate(scores), key=lambda x: -x[1])[:topk]
    return [{"idx": i, "score": float(s)} for i, s in ranked if s > 0]


def get_chunk(idx: int) -> dict:
    return _corpus[idx] if 0 <= idx < len(_corpus) else {}
