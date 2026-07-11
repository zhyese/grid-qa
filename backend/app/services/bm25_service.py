"""BM25 稀疏检索（rank-bm25 + jieba 分词）。内存维护全量 chunk 语料。

Milvus 2.4 用外部 BM25 + RRF 实现混合检索（避免依赖 Milvus 内置全文检索版本约束）。

优化：
- jieba 分词结果 lru_cache（query 高频复用，省重复分词）。
- 索引 pickle 落盘：进程/容器重启冷启动时加载盘上索引免全量重建（带 chunk 数校验防脏加载）。
- mark_dirty：文档增删后标记，下次 ensure_built（检索时）触发重建。

注：_dirty/bm25 为进程内状态，多 worker 部署时各 worker 独立（文档更新不频繁，可接受）。
"""
import pickle
from functools import lru_cache
from pathlib import Path
from typing import Optional

import jieba
from rank_bm25 import BM25Okapi
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.document import Document

_PKL_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "bm25_index.pkl"
_IDX_VERSION = 2  # 索引格式版本（结构变更时 +1，旧 pickle 自动失效）

_corpus: list[dict] = []          # [{text, doc_id, doc_name, chunk_idx}]
_tokenized: list[list[str]] = []
_bm25: Optional[BM25Okapi] = None
_dirty: bool = False              # 文档增删后置 True，下次 ensure_built 触发重建


@lru_cache(maxsize=100000)
def _tokenize(text: str) -> tuple[str, ...]:
    """jieba 分词 → token tuple（过滤空白）。tuple 可哈希供 lru_cache；query 高频复用。"""
    return tuple(w for w in jieba.cut(text or "") if w.strip())


def _save_pickle() -> None:
    try:
        _PKL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_PKL_PATH, "wb") as f:
            pickle.dump({
                "version": _IDX_VERSION,
                "corpus": _corpus,
                "tokenized": _tokenized,
                "bm25": _bm25,
            }, f)
    except Exception:
        pass  # 落盘失败不阻塞（纯加速优化）


async def _chunk_count(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count(Chunk.id)))).scalar() or 0)


async def _try_load_pickle(db: AsyncSession) -> bool:
    """加载盘上索引；版本/chunk 数校验通过返回 True，否则 False。"""
    global _corpus, _tokenized, _bm25
    try:
        if not _PKL_PATH.exists():
            return False
        with open(_PKL_PATH, "rb") as f:
            data = pickle.load(f)
        if data.get("version") != _IDX_VERSION:
            return False
        if len(data.get("corpus") or []) != await _chunk_count(db):
            return False  # 文档变动 → 脏加载，丢弃
        _corpus = data["corpus"]
        _tokenized = data["tokenized"]
        _bm25 = data["bm25"]
        return _bm25 is not None
    except Exception:
        return False


async def rebuild(db: AsyncSession) -> int:
    global _corpus, _tokenized, _bm25, _dirty
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
    _tokenized = [list(_tokenize(c.content)) for c, _ in rows]
    _bm25 = BM25Okapi(_tokenized) if _tokenized else None
    _dirty = False
    _save_pickle()
    return len(_corpus)


async def ensure_built(db: AsyncSession) -> None:
    global _dirty
    if _bm25 is not None and not _dirty:
        return
    # 冷启动优先加载盘上索引；脏或加载失败则全量重建
    if _bm25 is None and await _try_load_pickle(db):
        _dirty = False
        return
    await rebuild(db)


def mark_dirty() -> None:
    """文档增删后调用：标记 BM25 脏，下次 ensure_built（检索时）触发重建。"""
    global _dirty
    _dirty = True


def search(query: str, topk: int = 20) -> list[dict]:
    if not _bm25:
        return []
    scores = _bm25.get_scores(list(_tokenize(query)))
    ranked = sorted(enumerate(scores), key=lambda x: -x[1])[:topk]
    return [{"idx": i, "score": float(s)} for i, s in ranked if s > 0]


def get_chunk(idx: int) -> dict:
    return _corpus[idx] if 0 <= idx < len(_corpus) else {}
