"""已有表格块的精确行列查询。歧义/结构损坏时不猜测值。"""

import re

from sqlalchemy import select

from app.core.response import BizError
from app.models.chunk import Chunk
from app.models.knowledge_governance import KnowledgeDocumentMetadata
from app.services.ops_snapshot_service import readable_document
from datetime import datetime, timezone


def parse_table(text):
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    if len(lines) < 3:
        return None
    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines]
    if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in rows[1]):
        return None
    header = rows[0]
    if (
        not all(header)
        or len(set(header)) != len(header)
        or any(len(row) != len(header) for row in rows[2:])
    ):
        return None
    return header, rows[2:]


async def lookup(db, user, body):
    doc = await readable_document(db, user, body.doc_id)
    meta = (
        await db.execute(
            select(KnowledgeDocumentMetadata).where(
                KnowledgeDocumentMetadata.doc_id == doc.id,
                KnowledgeDocumentMetadata.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if meta and (
        meta.version_status != "active"
        or (meta.effective_at and meta.effective_at > now)
        or (meta.expires_at and meta.expires_at <= now)
    ):
        raise BizError("该文档未生效、已过期或非有效版本，不输出适用值", 400)
    chunks = (
        (
            await db.execute(
                select(Chunk)
                .where(Chunk.doc_id == doc.id, Chunk.chunk_type == "table")
                .order_by(Chunk.chunk_idx)
                .limit(501)
            )
        )
        .scalars()
        .all()
    )
    if len(chunks) > 500:
        raise BizError("表格超过500块，请按章节拆分文档", 400)
    matches, malformed = [], 0
    for chunk in chunks:
        parsed = parse_table(chunk.content)
        if parsed is None:
            malformed += 1
            continue
        header, rows = parsed
        if body.column not in header or any(k not in header for k in body.filters):
            continue
        for index, cells in enumerate(rows, start=2):
            row = dict(zip(header, cells))
            if all(row[k] == v for k, v in body.filters.items()):
                matches.append(
                    {
                        "value": row[body.column],
                        "column": body.column,
                        "row": row,
                        "rowNumber": index,
                        "docId": doc.id,
                        "docName": doc.doc_name,
                        "chunkId": chunk.id,
                        "page": chunk.page_num,
                        "section": chunk.section_path or chunk.section,
                        "tableText": chunk.content,
                    }
                )
    unique = len(matches) == 1 and malformed == 0 and bool(matches[0]["value"])
    return {
        "answer": matches[0]["value"]
        if unique
        else "证据不唯一、不完整或未命中，请核对条件及原表。",
        "status": "matched" if unique else "needs_review",
        "matches": matches,
        "malformedTables": malformed,
        "governanceVerified": meta is not None,
        "note": "列名与条件为精确匹配；单位保留在表头/单元格中，不自动换算。合并表头、表外注释及适用范围须查阅原文。",
    }
