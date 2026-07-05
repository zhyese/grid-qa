"""知识图谱服务：LLM 抽取三元组 → 双写 MySQL(统计/审计) + Neo4j(图查询/多跳推理)。

图查询与多跳推理走 Neo4j（影响链/枢纽分析）；Neo4j 未启动时 get_graph 回退 MySQL 一跳查询，
保证图谱页不崩。统计仍用 MySQL 三元组表。
"""
import json
import re

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import neo4j_client
from app.core.response import BizError
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.kg_triple import KgTriple
from app.core.obs import degraded
from app.providers.factory import get_llm_provider

_BATCH = 6  # 每批喂 LLM 的分块数（控制输入长度与抽取稳定性）

_KG_PROMPT_V2 = """你是电网运维知识图谱抽取器。从下面运维文本中抽取结构化三元组 (主体, 关系, 客体)。

【实体类型】只抽这些类型的名词短语作为主体/客体：
设备(主变压器/断路器/隔离开关/互感器/避雷器/电缆/母线/GIS等)、部件、故障现象、异常、处置措施、检修步骤、运行参数、危险点、保护装置、标准、系统。

【关系白名单】关系 r 只能从以下选一个；文本中不属于这些语义的【不要抽】：
发生 / 表现为 / 处置方法 / 检修步骤 / 原因 / 影响 / 预防 / 属于 / 位于 / 额定值 / 预警阈值 / 保护 / 试验

【严格要求】
1) 只抽文本中明确出现的事实，绝不编造。
2) 主体 s 与客体 o 必须是具体名词短语（设备名/现象/措施/参数），不得是章节号、"本文""本章"等泛指词。
3) 关系 r 必须在白名单内。
4) 输出严格 JSON 数组：[{{"s":"主体","r":"关系","o":"客体"}}, ...]；无合适三元组输出 []。不要解释、不要 markdown 代码块。

【好例】
文本：主变压器上层油温超过 95℃，应立即减负荷运行并检查冷却系统。
输出：[{{"s":"主变压器","r":"预警阈值","o":"上层油温95℃"}},{{"s":"主变压器","r":"处置方法","o":"减负荷运行"}}]

【坏例（禁止）】
{{"s":"第二章","r":"属于","o":"本文"}}  ← 章节号/泛指词，禁止

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


def _validate_triples(arr) -> list[dict]:
    """逐条校验：dict、s/r/o 非空、长度 ≤30。"""
    out = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        s = str(it.get("s", "")).strip()
        r = str(it.get("r", "")).strip()
        o = str(it.get("o", "")).strip()
        if s and r and o and len(s) <= 30 and len(r) <= 30 and len(o) <= 30:
            out.append({"s": s, "r": r, "o": o})
    return out


def _parse_triples_v2(ans: str) -> list[dict]:
    """解析 LLM 三元组输出：JSON 数组优先，行式回退，逐条校验丢弃坏条目。"""
    if not ans:
        return []
    m = re.search(r"\[.*\]", ans, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                return _validate_triples(arr)
        except Exception:
            pass
    # 行式回退：逐个 {...} 解析
    line_objs = []
    for frag in re.findall(r"\{[^{}]*\}", ans):
        try:
            line_objs.append(json.loads(frag))
        except Exception:
            pass
    return _validate_triples(line_objs)


_TRIVIAL_BLACK = ("本文", "本节", "本章", "章节", "附录", "摘要", "目录", "前言")
_TRIVIAL_PAT = re.compile(r"(^第[一二三四五六七八九十百0-9]+[章节条])|^[0-9]+(\.[0-9]+)+$|^[0-9]+$|^[图表][0-9一二三四五六七八九十]")


def _is_trivial(s: str) -> bool:
    """噪声判断：空/过短/章节标题/纯数字标点/黑名单词。"""
    if not s:
        return True
    s = s.strip()
    if len(s) < 2:
        return True
    if s in _TRIVIAL_BLACK:
        return True
    if _TRIVIAL_PAT.match(s):
        return True
    if re.fullmatch(r"[\d\.\s\-/,，。：:;；、]+", s):
        return True
    return False


def _normalize_triples(triples: list[dict]) -> list[dict]:
    """全局后处理：实体归一 + 关系白名单 + 去重(s,r,o) + 噪声过滤。"""
    from app.services.kg_normalize import canonical_entity, canonical_relation
    seen: set = set()
    out: list[dict] = []
    for tp in triples:
        if not isinstance(tp, dict):
            continue
        s = canonical_entity(str(tp.get("s", "")))
        r = canonical_relation(str(tp.get("r", "")))
        o = canonical_entity(str(tp.get("o", "")))
        if not (s and r and o):
            continue
        if s == o or _is_trivial(s) or _is_trivial(o):
            continue
        key = (s, r, o)
        if key in seen:
            continue
        seen.add(key)
        out.append({"s": s[:256], "r": r[:128], "o": o[:256]})
    return out


async def _extract_from_chunks(provider, chunks_text: list[str]) -> list[dict]:
    """分批调 LLM 抽取（schema 约束 prompt）→ 解析为原始三元组。单批失败降级。"""
    all_triples: list[dict] = []
    for i in range(0, len(chunks_text), _BATCH):
        batch = chunks_text[i:i + _BATCH]
        text = "\n\n".join(batch)
        try:
            ans = await provider.chat(
                [{"role": "user", "content": _KG_PROMPT_V2.format(text=text)}],
                temperature=0.1, max_tokens=3000,
            )
            all_triples.extend(_parse_triples_v2(ans))
        except Exception as e:
            degraded("kg_extract_batch", e)
    return all_triples


async def extract_triples(db: AsyncSession, doc_id: str, model_type: str | None = None) -> dict:
    """对某文档分块批量抽取三元组，增量写 MySQL + Neo4j（不清旧，仅追加新条目）。"""
    doc = (await db.execute(select(Document).where(Document.id == doc_id))).scalar_one_or_none()
    if not doc:
        raise BizError("文档不存在", 404)
    rows = (
        await db.execute(select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_idx))
    ).scalars().all()
    if not rows:
        raise BizError("文档尚未解析，请先解析", 400)

    provider = get_llm_provider(model_type)
    raw = await _extract_from_chunks(provider, [c.content for c in rows])
    triples = _normalize_triples(raw)

    # ★ 增量追加：查已有三元组，只插入新条目（不清旧）
    existing = (await db.execute(
        select(KgTriple.subject, KgTriple.relation, KgTriple.object)
        .where(KgTriple.doc_id == doc_id)
    )).all()
    existing_set = {(r[0], r[1], r[2]) for r in existing}
    new_triples = [t for t in triples if (t["s"], t["r"], t["o"]) not in existing_set]

    # 写 MySQL（仅新增，不删旧）
    for tp in new_triples:
        db.add(KgTriple(subject=tp["s"], relation=tp["r"], object=tp["o"],
                        doc_id=doc_id, doc_name=doc.doc_name))
    await db.commit()

    # 写 Neo4j（MERGE 幂等，仅新增边，不去旧；同时推断并落 Entity.type 供 3D 着色）
    if new_triples:
        from app.clients.neo4j_client import _infer_type
        for tp in new_triples:
            tp["s_type"] = _infer_type(tp["s"], tp["r"], as_subject=True)
            tp["o_type"] = _infer_type(tp["o"], tp["r"], as_subject=False)
        try:
            await neo4j_client.upsert_triples(new_triples, doc_id, doc.doc_name)
        except Exception as e:
            degraded("kg_neo4j_write", e)

    try:
        from app.core import metrics
        total = (await db.execute(select(func.count()).select_from(KgTriple))).scalar() or 0
        metrics.KG_EXTRACT.inc()
        metrics.KB_TRIPLES.set(total)
    except Exception:
        pass
    return {"tripleCount": len(triples), "newCount": len(new_triples),
            "docName": doc.doc_name, "sample": triples[:30]}


async def _get_graph_mysql(db: AsyncSession, entity: str, limit: int) -> dict:
    """MySQL 一跳查询（Neo4j 不可用时的回退）。"""
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
        "nodes": list(nodes.values()), "links": links,
        "categories": [{"name": "实体"}, {"name": "属性/关系"}], "total": len(rows),
    }


async def get_graph(db: AsyncSession, entity: str = "", limit: int = 300) -> dict:
    """关系图谱：优先 Neo4j 图查询，未启动则回退 MySQL 一跳。"""
    try:
        return await neo4j_client.get_neighbors(entity, limit)
    except Exception as e:
        degraded("kg_neo4j_fallback", e, "回退 MySQL")
        return await _get_graph_mysql(db, entity, limit)


async def get_paths(entity: str, depth: int = 3, limit: int = 20) -> list[dict]:
    """多跳影响链：设备→故障→处置→关联设备 的因果传播（仅 Neo4j）。"""
    try:
        return await neo4j_client.get_paths(entity, depth, limit)
    except Exception as e:
        degraded("kg_paths", e)
        return []


async def get_hubs(limit: int = 15) -> list[dict]:
    """枢纽实体：出度最高（影响传播源头，核心设备/故障，仅 Neo4j）。"""
    try:
        return await neo4j_client.get_hubs(limit)
    except Exception as e:
        degraded("kg_hubs", e)
        return []


async def graph_context(query: str, topk: int = 8) -> list[str]:
    """GraphRAG：从 query 提取关键词查 Neo4j 关联三元组，文本化作为问答结构化上下文。

    让问答"走 Neo4j"——检索文档分块之外，补充图谱结构化关系（设备-故障-处置链）。
    """
    import jieba
    words = [w for w in jieba.cut(query) if len(w.strip()) > 1]
    if not words:
        return []
    try:
        rows = await neo4j_client.query_triples_by_keywords(words, topk)
    except Exception as e:
        degraded("kg_graph_context", e)
        return []
    return [f"{r['s']} --{r['rel']}--> {r['o']}" for r in rows if r.get("s") and r.get("o")]


async def get_stats(db: AsyncSession) -> dict:
    """知识图谱统计（MySQL 三元组表）。"""
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
        "hubCount": await _hub_count(),
    }


async def _hub_count() -> int:
    """Neo4j 中有出度的实体数（可作为影响链传播源头的实体数）。"""
    try:
        from app.clients.neo4j_client import _get
        async with _get().session() as s:
            result = await s.run(
                "MATCH (n:Entity)-[r:REL]->() RETURN count(DISTINCT n) AS cnt"
            )
            rec = await result.single()
            return rec["cnt"] if rec else 0
    except Exception:
        return 0
