"""知识图谱服务：LLM 抽取三元组 / 关联查询 / 图数据导出。

抽取：分块批量喂 LLM，输出 JSON 三元组，清旧写新入 kg_triples 表。
图数据：按实体模糊匹配组装 echarts force 所需 nodes/links。
"""
import json
import re

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import BizError
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.kg_triple import KgTriple
from app.providers.factory import get_llm_provider

_BATCH = 6  # 每批喂 LLM 的分块数（控制输入长度与抽取稳定性）

_KG_PROMPT = """你是电网运维知识图谱抽取器。从下面这段运维文本中，抽取结构化三元组 (主体, 关系, 客体)。
抽取范围：设备/部件名称、故障/异常现象、处置/检修措施、运行参数、所属系统等。
关系尽量动词化，如：发生、表现为、处置方法、检修步骤、属于、位于、额定值、预警阈值。
严格要求：
1) 只抽取文本中明确出现的事实，绝不编造或脑补。
2) 输出严格 JSON 数组：[{{"s":"主体","r":"关系","o":"客体"}}, ...]，无则输出 []。
3) s/r/o 为简短中文短语（不超过 20 字），不要输出任何解释文字、不要用 markdown 代码块包裹。

【文本】
{text}"""


def _parse_triples(ans: str) -> list[dict]:
    """从 LLM 输出中解析三元组 JSON（容错：去注释/代码块/截断）。"""
    m = re.search(r"\[.*\]", ans or "", re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        s, r, o = str(it.get("s", "")).strip(), str(it.get("r", "")).strip(), str(it.get("o", "")).strip()
        if s and r and o and len(s) <= 20 and len(r) <= 20 and len(o) <= 20:
            out.append({"s": s[:256], "r": r[:128], "o": o[:256]})
    return out


async def extract_triples(db: AsyncSession, doc_id: str, model_type: str | None = None) -> dict:
    """对某文档分块批量抽取三元组，清旧写新。"""
    doc = (await db.execute(select(Document).where(Document.id == doc_id))).scalar_one_or_none()
    if not doc:
        raise BizError("文档不存在", 404)
    rows = (
        await db.execute(select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_idx))
    ).scalars().all()
    if not rows:
        raise BizError("文档尚未解析，请先解析", 400)

    provider = get_llm_provider(model_type)
    triples: list[dict] = []
    for i in range(0, len(rows), _BATCH):
        batch = rows[i:i + _BATCH]
        text = "\n\n".join(c.content for c in batch)
        try:
            ans = await provider.chat(
                [{"role": "user", "content": _KG_PROMPT.format(text=text)}],
                temperature=0.1, max_tokens=3000,
            )
            triples.extend(_parse_triples(ans))
        except Exception:
            continue  # 单批失败不中断整体抽取

    # 清旧 + 写新
    await db.execute(delete(KgTriple).where(KgTriple.doc_id == doc_id))
    for tp in triples:
        db.add(KgTriple(subject=tp["s"], relation=tp["r"], object=tp["o"],
                        doc_id=doc_id, doc_name=doc.doc_name))
    await db.commit()

    try:
        from app.core import metrics
        total = (await db.execute(select(func.count()).select_from(KgTriple))).scalar() or 0
        metrics.KG_EXTRACT.inc()
        metrics.KB_TRIPLES.set(total)
    except Exception:
        pass
    return {"tripleCount": len(triples), "docName": doc.doc_name, "sample": triples[:30]}


async def get_graph(db: AsyncSession, entity: str = "", limit: int = 300) -> dict:
    """按实体模糊匹配三元组，组装 echarts force 图数据。"""
    stmt = select(KgTriple)
    if entity:
        kw = f"%{entity}%"
        stmt = stmt.where(or_(KgTriple.subject.like(kw), KgTriple.object.like(kw)))
    stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).scalars().all()

    nodes: dict[str, dict] = {}
    links: list[dict] = []
    for t in rows:
        if t.subject not in nodes:
            nodes[t.subject] = {"id": t.subject, "name": t.subject, "category": 0, "symbolSize": 36}
        if t.object not in nodes:
            nodes[t.object] = {"id": t.object, "name": t.object, "category": 1, "symbolSize": 28}
        links.append({"source": t.subject, "target": t.object, "value": t.relation})
    return {
        "nodes": list(nodes.values()),
        "links": links,
        "categories": [{"name": "实体"}, {"name": "属性/关系"}],
        "total": len(rows),
    }


async def get_stats(db: AsyncSession) -> dict:
    """知识图谱统计：三元组/实体/关系类型数 + 文档分布 top。"""
    triple_total = (await db.execute(select(func.count()).select_from(KgTriple))).scalar() or 0
    entity_total = (await db.execute(
        select(func.count(func.distinct(KgTriple.subject)))
    )).scalar() or 0
    rel_total = (await db.execute(
        select(func.count(func.distinct(KgTriple.relation)))
    )).scalar() or 0
    by_doc = (await db.execute(
        select(KgTriple.doc_name, func.count())
        .group_by(KgTriple.doc_name).order_by(func.count().desc()).limit(10)
    )).all()
    try:
        from app.core import metrics
        metrics.KB_TRIPLES.set(triple_total)
    except Exception:
        pass
    return {
        "tripleTotal": triple_total,
        "entityTotal": entity_total,
        "relationTotal": rel_total,
        "byDoc": [{"docName": r[0] or "未知", "count": r[1]} for r in by_doc],
    }
