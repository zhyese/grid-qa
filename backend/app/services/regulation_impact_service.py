"""基于已解析原文的保守差异分析，不自动选择正确规程或改写票据。"""

import re
from difflib import SequenceMatcher

from sqlalchemy import select

from app.core.response import BizError
from app.models.chunk import Chunk
from app.models.ticket import Ticket, TicketStatus
from app.services.ops_snapshot_service import create_snapshot, readable_document


def evidence(chunk):
    return {
        "docId": chunk.doc_id,
        "chunkId": chunk.id,
        "page": chunk.page_num,
        "section": chunk.section_path or chunk.section or "",
        "text": chunk.content,
    }


def compare_chunks(old, new):
    """顺序对齐；修改块保留双侧完整证据，不把块序号当作条款编号。"""
    matcher = SequenceMatcher(
        None, [c.content for c in old], [c.content for c in new], autojunk=False
    )
    changes = []
    for tag, a, b, c, d in matcher.get_opcodes():
        if tag == "equal":
            continue
        before, after = old[a:b], new[c:d]
        left = re.findall(
            r"[-+]?\d+(?:\.\d+)?\s*(?:℃|°C|kV|kA|MPa|Hz|%|mm|分钟|小时)?",
            "\n".join(x.content for x in before),
        )
        right = re.findall(
            r"[-+]?\d+(?:\.\d+)?\s*(?:℃|°C|kV|kA|MPa|Hz|%|mm|分钟|小时)?",
            "\n".join(x.content for x in after),
        )
        changes.append(
            {
                "type": tag,
                "before": [evidence(x) for x in before],
                "after": [evidence(x) for x in after],
                "numbersChanged": left != right,
                "oldNumbers": left,
                "newNumbers": right,
            }
        )
    return changes


async def analyze(db, user, body):
    if body.old_doc_id == body.new_doc_id:
        raise BizError("请选两份不同的已解析文档；历史文件版本请单独上传后比较", 400)
    old_doc = await readable_document(db, user, body.old_doc_id)
    new_doc = await readable_document(db, user, body.new_doc_id)
    groups = []
    for doc in (old_doc, new_doc):
        chunks = (
            (
                await db.execute(
                    select(Chunk)
                    .where(Chunk.doc_id == doc.id)
                    .order_by(Chunk.chunk_idx, Chunk.id)
                    .limit(2001)
                )
            )
            .scalars()
            .all()
        )
        if not chunks:
            raise BizError("文档尚未解析或没有可比较文本", 400)
        if len(chunks) > 2000 or sum(len(c.content) for c in chunks) > 500_000:
            raise BizError("文档超过同步比较上限（2000块/50万字符），请拆分章节", 400)
        groups.append(chunks)
    changes = compare_chunks(*groups)
    tickets = (
        (
            await db.execute(
                select(Ticket)
                .where(
                    Ticket.tenant_id == user.tenant_id,
                    Ticket.is_deleted == 0,
                    Ticket.status.notin_(
                        [
                            TicketStatus.COMPLETED,
                            TicketStatus.ARCHIVED,
                            TicketStatus.REJECTED,
                        ]
                    ),
                )
                .order_by(Ticket.id)
                .limit(1001)
            )
        )
        .scalars()
        .all()
    )
    if len(tickets) > 1000:
        raise BizError("未结束票据超过1000条，当前同步影响扫描无法保证完整性", 400)
    candidates = []
    for ticket in tickets:
        text = "\n".join(
            [
                ticket.notes or "",
                ticket.audit_report or "",
                ticket.steps or "",
                ticket.safety_measures or "",
            ]
        )
        if old_doc.id in text or old_doc.doc_name in text:
            candidates.append(
                {
                    "ticketId": ticket.id,
                    "title": ticket.title,
                    "basis": "票据文本出现旧文档ID或名称，需人工核实引用及适用条款",
                    "confidence": "candidate",
                }
            )
    payload = {
        "documentIds": [old_doc.id, new_doc.id],
        "oldName": old_doc.doc_name,
        "newName": new_doc.doc_name,
        "changes": changes,
        "ticketCandidates": candidates,
        "scope": "仅比较已解析文本；分块变化可能引入假差异。数值变化也可能是编号变化，并非自动认定阈值变化。",
        "limitations": "未建立引用关系的知识条目/票据不在确定影响范围；不自动修改规程、缓存或票据。",
    }
    return await create_snapshot(
        db, user, "impact", f"{old_doc.doc_name} → {new_doc.doc_name}"[:200], payload
    )
