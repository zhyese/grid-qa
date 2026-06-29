"""Neo4j 异步驱动封装（知识图谱：设备-故障-处置 多跳推理）。

节点 :Entity {name}；统一有向关系 :REL {type, doc_id, doc_name}（type 承载语义：
发生/表现为/处置方法/属于…）。固定关系类型保证多跳 [:REL*1..N] 通用查询，
避免 Cypher 关系类型不能参数化、中文关系类型不优雅的问题。
"""
from neo4j import AsyncGraphDatabase

from app.config import settings

_driver = None


def _get():
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
        )
    return _driver


async def close():
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


async def ensure_constraint():
    """建索引（幂等）：加速按 name 查节点。Neo4j 未启动时抛异常，由调用方兜底。"""
    async with _get().session() as s:
        await s.run("CREATE INDEX entity_name IF NOT EXISTS FOR (n:Entity) ON (n.name)")


async def upsert_triples(triples: list[dict], doc_id: str, doc_name: str) -> int:
    """批量 MERGE 写入三元组（幂等，重复抽取不产生重复边）。"""
    if not triples:
        return 0
    async with _get().session() as s:
        for t in triples:
            await s.run(
                """
                MERGE (a:Entity {name: $s})
                MERGE (b:Entity {name: $o})
                MERGE (a)-[r:REL {type: $r}]->(b)
                  ON CREATE SET r.doc_id = $doc_id, r.doc_name = $doc_name
                """,
                s=t["s"], r=t["r"], o=t["o"], doc_id=doc_id, doc_name=doc_name,
            )
    return len(triples)


async def delete_by_doc(doc_id: str) -> None:
    """删除某文档产生的边（重新抽取前清旧）。"""
    async with _get().session() as s:
        await s.run("MATCH (:Entity)-[r:REL {doc_id: $doc_id}]->() DELETE r", doc_id=doc_id)


async def get_neighbors(entity: str = "", limit: int = 300) -> dict:
    """按实体模糊查邻居子图（echarts force：nodes + links）。"""
    async with _get().session() as s:
        if entity:
            result = await s.run(
                """
                MATCH (n:Entity)-[r:REL]-(m:Entity)
                WHERE n.name CONTAINS $entity
                RETURN n.name AS s, r.type AS rel, m.name AS o LIMIT $limit
                """,
                entity=entity, limit=limit,
            )
        else:
            result = await s.run(
                "MATCH (a:Entity)-[r:REL]->(b:Entity) "
                "RETURN a.name AS s, r.type AS rel, b.name AS o LIMIT $limit",
                limit=limit,
            )
        rows = [rec async for rec in result]
    nodes: dict[str, dict] = {}
    links: list[dict] = []
    for row in rows:
        s_, o_ = row["s"], row["o"]
        if s_ and s_ not in nodes:
            nodes[s_] = {"id": s_, "name": s_, "category": 0, "symbolSize": 36}
        if o_ and o_ not in nodes:
            nodes[o_] = {"id": o_, "name": o_, "category": 1, "symbolSize": 28}
        links.append({"source": s_, "target": o_, "value": row["rel"]})
    return {
        "nodes": list(nodes.values()),
        "links": links,
        "categories": [{"name": "实体"}, {"name": "属性/关系"}],
        "total": len(rows),
    }


async def get_paths(entity: str, depth: int = 3, limit: int = 20) -> list[dict]:
    """多跳影响链：从 entity 出发的 depth 跳有向路径（故障传播/影响分析）。"""
    depth = max(1, min(int(depth), 5))   # 强制 int 防 Cypher 注入 + 限深
    async with _get().session() as s:
        result = await s.run(
            f"MATCH path = (n:Entity)-[:REL*1..{depth}]->(m:Entity) "
            "WHERE n.name CONTAINS $entity "
            "RETURN [x IN nodes(path) | x.name] AS chain, "
            "[r IN relationships(path) | r.type] AS rels, length(path) AS hops "
            "ORDER BY hops LIMIT $limit",
            entity=entity, limit=limit,
        )
        rows = [rec async for rec in result]
    return [{"chain": rec["chain"], "rels": rec["rels"], "hops": rec["hops"]} for rec in rows]


async def get_hubs(limit: int = 15) -> list[dict]:
    """枢纽实体：出度最高（影响传播的源头，核心设备/故障）。"""
    async with _get().session() as s:
        result = await s.run(
            "MATCH (n:Entity)-[r:REL]->() "
            "RETURN n.name AS name, count(r) AS outDegree "
            "ORDER BY outDegree DESC LIMIT $limit",
            limit=limit,
        )
        rows = [rec async for rec in result]
    return [{"name": rec["name"], "outDegree": rec["outDegree"]} for rec in rows]


async def query_triples_by_keywords(words: list[str], limit: int = 8) -> list[dict]:
    """查 name 含任一关键词的实体一跳三元组（GraphRAG 问答增强用）。"""
    if not words:
        return []
    async with _get().session() as s:
        result = await s.run(
            """
            UNWIND $words AS w
            MATCH (n:Entity)-[r:REL]->(m:Entity)
            WHERE n.name CONTAINS w OR m.name CONTAINS w
            RETURN DISTINCT n.name AS s, r.type AS rel, m.name AS o
            LIMIT $limit
            """,
            words=words, limit=limit,
        )
        rows = [rec async for rec in result]
    return [{"s": rec["s"], "rel": rec["rel"], "o": rec["o"]} for rec in rows]
