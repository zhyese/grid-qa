"""数据飞轮·A3+A4 治理联动订阅：doc_blocked → 联动清理存储层。

handler 触发路径：
1. Milvus 软删（A3）：milvus_client.delete_by_doc 双 collection（grid_chunks + grid_chunks_bge）
2. Neo4j 清（A4）：复用 neo4j_client.delete_by_doc + MySQL kg_triples 按 doc_id 删
3. qa_cache 反向失效（A4）：扫 qa_cache 表 retrieval_sources / answer JSON 含 docId 行删 + Redis qa:* 同 key 失效

开关 GOVERNANCE_PROPAGATE_ENABLE 默认关（关=仅检索时过滤现状，零破坏）。
异常各路径独立 degraded 不阻塞订阅总线；handler 异常 quality_event_bus 自身已兜底。
import 副作用注册 subscribe（幂等，仿 evidence_gap_service）。
"""
from app.clients import milvus_client, redis_client
from app.config import settings
from app.core.obs import degraded
from app.db.session import AsyncSessionLocal


async def propagate_handler(event_id, source, type, payload, tenant):
    """订阅 governance.doc_blocked → 联动清理 Milvus / Neo4j / qa_cache。

    幂等：同一 doc_id 反复触发，各清理路径自身幂等（Milvus delete expr / MySQL delete / Redis del）。
    """
    if not getattr(settings, "GOVERNANCE_PROPAGATE_ENABLE", False):
        return  # opt-in 默认关
    doc_id = (payload or {}).get("doc_id")
    if not doc_id:
        return
    reason = (payload or {}).get("reason", "unknown")

    # A3 Milvus 软删（双 collection）
    try:
        milvus_client.delete_by_doc(doc_id)
        _inc_metric("milvus")
    except Exception as e:
        degraded("governance_propagate_milvus", e)

    # A4 Neo4j 清（按 doc_id）
    try:
        await _purge_neo4j_for_doc(doc_id)
        _inc_metric("neo4j")
    except Exception as e:
        degraded("governance_propagate_neo4j", e)

    # A4 qa_cache 反向失效（扫表 + Redis）
    try:
        await _invalidate_qa_cache_for_doc(doc_id)
        _inc_metric("qa_cache")
    except Exception as e:
        degraded("governance_propagate_qa_cache", e)

    # 治理代际 +1（A5 cv G 段，所有 qa 缓存 key 失效）
    try:
        await _bump_gov_generation()
    except Exception as e:
        degraded("governance_propagate_bump_gen", e)

    # C3 度量：联动清理计数
    try:
        from app.core import metrics
        metrics.GOVERNANCE_PROPAGATED.labels(reason).inc()
    except Exception:
        pass


async def _purge_neo4j_for_doc(doc_id: str) -> None:
    """按 doc_id 清 Neo4j 边 + MySQL kg_triples（复用 document_service.delete_document 范式）。"""
    from sqlalchemy import delete as _del, select
    from app.models.kg_triple import KgTriple
    async with AsyncSessionLocal() as db:
        await db.execute(_del(KgTriple).where(KgTriple.doc_id == doc_id))
        await db.commit()
    try:
        from app.clients import neo4j_client
        await neo4j_client.delete_by_doc(doc_id)
    except Exception as e:
        degraded("governance_propagate_neo4j_by_doc", e)


async def _invalidate_qa_cache_for_doc(doc_id: str) -> None:
    """扫 qa_cache 表，retrieval_sources / answer JSON 含 docId 的行删 + Redis qa:* 同 key 失效。

    retrieval_sources 是 JSON 列；answer 是 JSON 字符串。两者任一含 docId 即删。
    用 JSON_CONTAINS / LIKE 兜底方言兼容。
    """
    from sqlalchemy import delete as _del, select, text
    from app.models.qa_cache import QaCache
    async with AsyncSessionLocal() as db:
        # MySQL JSON_CONTAINS 兼容；非 MySQL 走 LIKE 兜底（answer/retrieval_sources 文本含 docId）
        try:
            rows = (await db.execute(text(
                "SELECT id, cache_key, answer, retrieval_sources FROM qa_cache "
                "WHERE answer LIKE :kw OR retrieval_sources LIKE :kw"
            ), {"kw": f'%"{doc_id}"%'})).all()
        except Exception as e:
            degraded("governance_propagate_qa_cache_scan", e)
            return
        if not rows:
            return
        # Redis 同 key 失效
        for r in rows:
            try:
                cache_key = r.cache_key if not isinstance(r, tuple) else r[1]
                await redis_client.get_redis().delete(cache_key)
            except Exception:
                pass
        # MySQL 行软/硬删（治理场景倾向硬删：旧 doc 已不可检索，缓存永久脏）
        ids = [r.id if not isinstance(r, tuple) else r[0] for r in rows]
        try:
            await db.execute(_del(QaCache).where(QaCache.id.in_(ids)))
            await db.commit()
        except Exception as e:
            degraded("governance_propagate_qa_cache_del", e)


async def _bump_gov_generation() -> None:
    """治理代际 +1（A5 cv G 段）。Redis key qa:gov_gen int 计数器。

    bump 后 cv 变 → 所有 qa:* cache key 变 → 旧 key 自动 miss（缓存雪崩防护见 spec §9）。
    """
    try:
        await redis_client.get_redis().incr("qa:gov_gen")
    except Exception as e:
        degraded("governance_propagate_gov_gen", e)


def _inc_metric(_action: str) -> None:
    """C3 度量埋点占位（C3 task 补全指标注册）。"""
    try:
        from app.core import metrics
        metrics.QUALITY_EVENT_TOTAL.labels("governance_propagate", _action).inc()
    except Exception:
        pass


def _register_quality_bus() -> None:
    """注册质量事件订阅（幂等，import 时调一次；quality_event_bus 未就绪则跳过）。"""
    try:
        from app.services.quality_event_bus import subscribe
        subscribe("governance.doc_blocked", propagate_handler)
    except Exception:
        pass


_register_quality_bus()  # import 副作用注册（被 quality_event_bus emit 后异步派发）
