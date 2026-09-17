"""一次性：从 MySQL KgTriple 镜像全量重建 Neo4j 图谱。

背景（2026-09-17）：宿主机断电致 Neo4j 事务日志损坏；抢救启动后库内仅存 61 条 REL 边，
断电期间抽取走降级只落 MySQL，镜像已积累 11052 条。本脚本按 doc 分组幂等 MERGE 回 Neo4j。

用法（容器内，复用后端 env/驱动）：
    docker exec grid-backend python /tmp/rebuild_kg_from_mysql.py
成功输出：REBUILD_OK docs=<n> triples=<n>；失败非零退出。
"""
import asyncio
import sys
from collections import defaultdict

sys.path.insert(0, "/app")

from sqlalchemy import select  # noqa: E402

from app.clients import neo4j_client  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.kg_triple import KgTriple  # noqa: E402


async def main() -> int:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(KgTriple))).scalars().all()
    if not rows:
        print("REBUILD_EMPTY mysql mirror has no triples")
        return 1
    by_doc: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in rows:
        by_doc[(t.doc_id or "", t.doc_name or "")].append(
            {"s": t.subject, "r": t.relation, "o": t.object}
        )
    await neo4j_client.ensure_constraint()
    total = 0
    for (doc_id, doc_name), triples in by_doc.items():
        total += await neo4j_client.upsert_triples(triples, doc_id, doc_name)
    await neo4j_client.close()
    print(f"REBUILD_OK docs={len(by_doc)} triples={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
