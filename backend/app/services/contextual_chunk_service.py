"""H2 切块上下文化（Contextual Retrieval）。

问题：结构感知分块解决了"结构"（父子/表格/章节），但 chunk 一旦脱离原文，
代词（"该设备""上述限值"）与省略主语会让向量语义失真，检索容易漏。

做法：入库/向量化时为每个 chunk 用 LLM 生成一句 50~100 字"文档定位语"
（所属文档/章节/主题/涉及设备），prepend 后再做 embedding。

关键约定（对齐调研报告 v5 H2）：
- **chunk 原文不变**：`Chunk.content` 与 Milvus 里存的检索展示文本都还是原文，
  只有送进 embedding 的文本被拼上定位语 → 生成侧零影响、引用原文零影响。
- **默认关**（`CONTEXTUAL_CHUNK_ENABLE=False`）= 现状；只影响入库/向量化链路，
  问答主链路零改动。
- 成本护栏：单文档条数上限 + 并发上限 + 超时；任一 chunk 失败只降级为空定位语，
  绝不阻塞向量化（仓库铁律：degrade, never crash）。
- 与 H1 共用同一次 Milvus 重建：先开开关再跑 `scripts/rebuild_embeddings.py`。
"""
from __future__ import annotations

import asyncio
import time

from app.config import settings
from app.core.obs import degraded


def enabled() -> bool:
    return bool(getattr(settings, "CONTEXTUAL_CHUNK_ENABLE", False))


def embed_text_for(content: str, context_line: str) -> str:
    """拼接送进 embedding 的文本：定位语 + 换行 + 原文（定位语为空则原文）。"""
    ctx = (context_line or "").strip()
    body = content or ""
    return f"{ctx}\n{body}" if ctx else body


async def _generate_one(model_type: str | None, doc_name: str, section: str,
                        content: str) -> str:
    """生成单条定位语；失败/超时返回空串（调用方按"无定位语"降级）。"""
    from app.providers.factory import get_llm_provider
    from app.rag import prompt_templates

    timeout = float(getattr(settings, "CONTEXTUAL_CHUNK_TIMEOUT", 20.0))
    max_tokens = int(getattr(settings, "CONTEXTUAL_CHUNK_MAX_TOKENS", 160))
    try:
        msgs = prompt_templates.build_contextual_chunk_messages(doc_name, section, content)
        prov = get_llm_provider(model_type)
        text = await asyncio.wait_for(
            prov.chat(msgs, temperature=0.1, max_tokens=max_tokens), timeout=timeout,
        )
        return (text or "").strip().strip('"').strip("“”")
    except Exception as e:
        degraded("contextual_chunk_generate", e, (doc_name or "")[:40])
        return ""


async def ensure_context_lines(db, doc, chunks, model_type: str | None = None) -> int:
    """为尚无定位语的 chunk 批量生成并落库，返回本次生成条数。

    chunks: Chunk ORM 对象序列（按 chunk_idx 有序）。已有点位跳过（幂等，重跑不重复花钱）。
    """
    if not enabled():
        return 0
    targets = [c for c in (chunks or []) if not (getattr(c, "context_line", "") or "").strip()]
    if not targets:
        return 0
    cap = int(getattr(settings, "CONTEXTUAL_CHUNK_MAX_PER_DOC", 500))
    if cap > 0:
        targets = targets[:cap]
    if not targets:
        return 0

    sem = asyncio.Semaphore(max(1, int(getattr(settings, "CONTEXTUAL_CHUNK_CONCURRENCY", 3))))
    doc_name = getattr(doc, "doc_name", "") or ""
    _t0 = time.time()

    async def _one(chunk):
        section = (getattr(chunk, "section_path", "") or getattr(chunk, "section", "") or "")
        async with sem:
            line = await _generate_one(model_type, doc_name, section, chunk.content or "")
        return chunk, line

    results = await asyncio.gather(*[_one(c) for c in targets], return_exceptions=True)
    filled = 0
    for item in results:
        if isinstance(item, BaseException):
            degraded("contextual_chunk_task", item)
            continue
        chunk, line = item
        if line:
            chunk.context_line = line
            filled += 1
    if filled:
        await db.commit()
    try:
        from app.core import metrics
        metrics.CONTEXTUAL_CHUNK_GENERATED.inc(filled)
        metrics.CONTEXTUAL_CHUNK_LATENCY.observe(time.time() - _t0)
    except Exception:
        pass
    return filled


async def stats(db) -> dict:
    """上下文化覆盖率（供运维/看板查看）。"""
    from sqlalchemy import func, select

    from app.models.chunk import Chunk
    total = (await db.execute(select(func.count()).select_from(Chunk))).scalar() or 0
    covered = (await db.execute(
        select(func.count()).select_from(Chunk).where(Chunk.context_line != "")
    )).scalar() or 0
    return {
        "enabled": enabled(),
        "chunkTotal": int(total),
        "contextualized": int(covered),
        "coverage": round(covered / total, 4) if total else 0.0,
    }
