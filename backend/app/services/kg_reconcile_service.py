"""KG 每日对账：MySQL KgTriple 镜像 vs Neo4j :REL 边数。

背景（2026-09-17 实锤事故）：断电后 Neo4j 崩溃循环期间，图谱抽取走降级只落
MySQL（KgTriple 11052 条），Neo4j 仅存 61 条边——漂移 170 倍，三个月内无任何
指标/告警/对账发现，直到人工 cypher 对账。本任务把对账变成默认开启的后台 cron。

口径：MySQL 端取 DISTINCT(subject, relation, object)（同三元组跨文档重复抽取
在 MySQL 保留多行、Neo4j MERGE 幂等去重为一边，故与边数比必须去重）。
漂移 = mysql_distinct - neo4j_edges；> KG_RECONCILE_DRIFT_WARN 时 WARN 日志
（Grafana 面板按 grid_kg_drift_triples > 0 告警）。
"""
import asyncio
import logging

from sqlalchemy import text

from app.core.obs import degraded

logger = logging.getLogger("app")

# 告警阈值：漂移条数（绝对值，非比例——图谱规模小，绝对值更直观）
DRIFT_WARN_THRESHOLD = 5


async def _mysql_distinct() -> int:
    """MySQL 镜像 DISTINCT 三元组数。独立 session（cron 语境无请求级 session）。"""
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        return (await db.execute(text(
            "SELECT COUNT(*) FROM (SELECT DISTINCT subject, relation, object FROM kg_triples) t"
        ))).scalar() or 0


async def reconcile_once() -> dict:
    """单次对账。返回 {"mysql": n, "neo4j": n, "drift": n, "warn": bool}。"""
    from app.clients import neo4j_client
    from app.core.metrics import KG_RECONCILE, KG_DRIFT_TRIPLES

    mysql_n = await _mysql_distinct()
    neo4j_n = await neo4j_client.count_rel_edges()
    drift = mysql_n - neo4j_n
    warn = abs(drift) > DRIFT_WARN_THRESHOLD
    KG_RECONCILE.labels(store="mysql_distinct").set(mysql_n)
    KG_RECONCILE.labels(store="neo4j_edges").set(neo4j_n)
    KG_DRIFT_TRIPLES.set(drift)
    if warn:
        logger.warning(
            "[kg-reconcile] 图谱漂移告警：MySQL DISTINCT=%d vs Neo4j 边=%d（漂移 %d）。"
            "修复路径：docker exec grid-backend python /tmp/rebuild_kg_from_mysql.py"
            "（或 scripts/rebuild_kg_from_mysql.py 重建）", mysql_n, neo4j_n, drift)
    else:
        logger.info("[kg-reconcile] 对账通过：MySQL DISTINCT=%d = Neo4j 边=%d", mysql_n, neo4j_n)
    return {"mysql": mysql_n, "neo4j": neo4j_n, "drift": drift, "warn": warn}


async def reconcile_loop(hours: float = 24.0) -> None:
    """周期对账 loop（lifespan 启动；KG_RECONCILE_CRON_HOURS<=0 关闭）。降级不中断。"""
    interval = max(hours, 0.1) * 3600
    while True:
        try:
            await reconcile_once()
        except Exception as e:
            degraded("kg_reconcile", e)
        await asyncio.sleep(interval)
