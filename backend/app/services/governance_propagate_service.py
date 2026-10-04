"""数据飞轮·A3+A4 治理联动订阅：doc_blocked → 联动清理存储层。

handler 触发路径（GOVERNANCE_PROPAGATE_ENABLE=true 时）：
1. Milvus 软删（A3）：milvus_client.delete_by_doc 双 collection（grid_chunks + grid_chunks_bge）
2. Neo4j 清（A4）：复用 neo4j_client.delete_by_doc + MySQL kg_triples 按 doc_id 删
3. qa_cache 反向失效（A4）：扫 qa_cache 表 retrieval_sources / answer JSON 含 docId 行删 + Redis qa:* 同 key 失效

A4 dry-run 安全阀（GOVERNANCE_PROPAGATE_DRY_RUN_ENABLE=true）：
    先只算"将清理什么"的候选报告（Milvus/Neo4j/kg_triples/qa_cache 数量 + 缓存 key 预览），
    以 governance.cleanup_dry_run 质量事件落库供人工评审，**不执行任何删除**；
    评审后经 POST /system/governance-propagate/execute 显式确认才走真实清理。
    （三线调研 A4 验收口径：dry-run 展示将清理的向量、图谱、缓存数量。）

开关 GOVERNANCE_PROPAGATE_ENABLE 默认关（关=仅检索时过滤现状，零破坏）；
DRY_RUN 默认关（关=事件直接触发真实清理，即原行为）。
异常各路径独立 degraded 不阻塞订阅总线；handler 异常 quality_event_bus 自身已兜底。
import 副作用注册 subscribe（幂等，仿 evidence_gap_service）。
"""
from app.clients import milvus_client, redis_client
from app.config import settings
from app.core.obs import degraded
from app.db.session import AsyncSessionLocal


async def build_cleanup_candidates(doc_id: str) -> dict:
    """统计 doc_id 关联的可清理对象（只读，不删）。

    返回 {milvus: {collection: n}, neo4jEdges: n, kgTriples: n,
          qaCacheRows: n, cacheKeyPreview: [...], totalEstimate: n}
    任一存储不可达计 0（degraded），不阻塞报告生成。
    """
    # 1) Milvus 双 collection 计数
    try:
        milvus_counts = milvus_client.count_by_doc(doc_id)
    except Exception as e:
        degraded("governance_dryrun_milvus", e)
        milvus_counts = {}
    # 2) Neo4j 边计数
    try:
        from app.clients import neo4j_client
        neo4j_edges = await neo4j_client.count_by_doc(doc_id)
    except Exception as e:
        degraded("governance_dryrun_neo4j", e)
        neo4j_edges = 0
    # 3) MySQL kg_triples + qa_cache 扫描（只 SELECT）
    kg_triples = 0
    try:
        from sqlalchemy import func, select
        from app.models.kg_triple import KgTriple
        async with AsyncSessionLocal() as db:
            kg_triples = int((await db.execute(
                select(func.count()).select_from(KgTriple).where(KgTriple.doc_id == doc_id)
            )).scalar() or 0)
    except Exception as e:
        degraded("governance_dryrun_kg_triples", e)
    qa_cache_rows, key_preview = 0, []
    try:
        from sqlalchemy import text
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(text(
                "SELECT id, cache_key, answer, retrieval_sources FROM qa_cache "
                "WHERE answer LIKE :kw OR retrieval_sources LIKE :kw"
            ), {"kw": f'%"{doc_id}"%'})).all()
        qa_cache_rows = len(rows)
        for r in rows[:20]:
            key_preview.append(r.cache_key if not isinstance(r, tuple) else r[1])
    except Exception as e:
        degraded("governance_dryrun_qa_cache_scan", e)
    milvus_total = sum(milvus_counts.values())
    return {
        "milvus": milvus_counts,
        "neo4jEdges": neo4j_edges,
        "kgTriples": kg_triples,
        "qaCacheRows": qa_cache_rows,
        "cacheKeyPreview": key_preview,
        "totalEstimate": milvus_total + neo4j_edges + kg_triples + qa_cache_rows,
    }


async def execute_propagate(doc_id: str, reason: str = "unknown") -> dict:
    """真实清理（dry-run 评审通过后由 execute 端点或 handler 调用）。返回各路径执行结果。

    幂等：同一 doc_id 反复触发，各清理路径自身幂等（Milvus delete expr / MySQL delete / Redis del）。
    """
    executed: dict = {}
    # A3 Milvus 软删（双 collection）
    try:
        milvus_client.delete_by_doc(doc_id)
        executed["milvus"] = True
        _inc_metric("milvus")
    except Exception as e:
        degraded("governance_propagate_milvus", e)
        executed["milvus"] = False

    # A4 Neo4j 清（按 doc_id）
    try:
        await _purge_neo4j_for_doc(doc_id)
        executed["neo4j"] = True
        _inc_metric("neo4j")
    except Exception as e:
        degraded("governance_propagate_neo4j", e)
        executed["neo4j"] = False

    # A4 qa_cache 反向失效（扫表 + Redis）
    try:
        executed["qaCacheDeleted"] = await _invalidate_qa_cache_for_doc(doc_id)
        _inc_metric("qa_cache")
    except Exception as e:
        degraded("governance_propagate_qa_cache", e)
        executed["qaCacheDeleted"] = 0

    # 治理代际 +1（A5 cv G 段，所有 qa 缓存 key 失效）
    try:
        await _bump_gov_generation()
        executed["govGenerationBumped"] = True
    except Exception as e:
        degraded("governance_propagate_bump_gen", e)
        executed["govGenerationBumped"] = False

    # C3 度量：联动清理计数
    try:
        from app.core import metrics
        metrics.GOVERNANCE_PROPAGATED.labels(reason).inc()
    except Exception:
        pass
    return executed


async def propagate_handler(event_id, source, type, payload, tenant):
    """订阅 governance.doc_blocked → dry-run 模式产出候选报告，否则直接联动清理。"""
    if not getattr(settings, "GOVERNANCE_PROPAGATE_ENABLE", False):
        return  # opt-in 默认关
    doc_id = (payload or {}).get("doc_id")
    if not doc_id:
        return
    reason = (payload or {}).get("reason", "unknown")

    # A4 dry-run 安全阀：只出候选报告（落质量事件），不动任何存储
    if getattr(settings, "GOVERNANCE_PROPAGATE_DRY_RUN_ENABLE", False):
        try:
            candidates = await build_cleanup_candidates(doc_id)
            from app.services import quality_event_bus
            await quality_event_bus.emit(
                "governance", "cleanup_dry_run",
                {"doc_id": doc_id, "reason": reason,
                 "hint": "候选清理报告（dry-run，未执行）。确认后调 POST /system/governance-propagate/execute",
                 "candidates": candidates},
                tenant=tenant or "default",
            )
            try:
                from app.core import metrics
                metrics.QUALITY_EVENT_TOTAL.labels("governance_dry_run", "report").inc()
            except Exception:
                pass
        except Exception as e:
            degraded("governance_dry_run_report", e)
        return

    await execute_propagate(doc_id, reason)


async def _purge_neo4j_for_doc(doc_id: str) -> None:
    """按 doc_id 清 Neo4j 边 + MySQL kg_triples（复用 document_service.delete_document 范式）。"""
    from sqlalchemy import delete as _del
    from app.models.kg_triple import KgTriple
    async with AsyncSessionLocal() as db:
        await db.execute(_del(KgTriple).where(KgTriple.doc_id == doc_id))
        await db.commit()
    try:
        from app.clients import neo4j_client
        await neo4j_client.delete_by_doc(doc_id)
    except Exception as e:
        degraded("governance_propagate_neo4j_by_doc", e)


async def _invalidate_qa_cache_for_doc(doc_id: str) -> int:
    """扫 qa_cache 表，retrieval_sources / answer JSON 含 docId 的行删 + Redis qa:* 同 key 失效。

    retrieval_sources 是 JSON 列；answer 是 JSON 字符串。两者任一含 docId 即删。
    用 LIKE 兜底方言兼容。返回删除行数。
    """
    from sqlalchemy import delete as _del, text
    from app.models.qa_cache import QaCache
    async with AsyncSessionLocal() as db:
        # MySQL LIKE 全文兜底（answer/retrieval_sources 文本含 docId）
        try:
            rows = (await db.execute(text(
                "SELECT id, cache_key, answer, retrieval_sources FROM qa_cache "
                "WHERE answer LIKE :kw OR retrieval_sources LIKE :kw"
            ), {"kw": f'%"{doc_id}"%'})).all()
        except Exception as e:
            degraded("governance_propagate_qa_cache_scan", e)
            return 0
        if not rows:
            return 0
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
            return 0
        return len(ids)


async def _bump_gov_generation() -> None:
    """治理代际 +1（A5 cv G 段）。Redis qa:gov_gen 持久化 + 进程内存镜像同步。

    bump 后 cv 变 → 所有 qa:* cache key 变 → 旧 key 自动 miss（缓存雪崩防护见 spec §9）。
    """
    # 进程内存镜像（citation_cache_version 同步读这个）
    try:
        from app.config import bump_gov_generation_inproc
        bump_gov_generation_inproc()
    except Exception as e:
        degraded("governance_propagate_gov_gen_inproc", e)
    # Redis 持久化（跨进程/重启保真）
    try:
        await redis_client.get_redis().incr("qa:gov_gen")
    except Exception as e:
        degraded("governance_propagate_gov_gen", e)


def _inc_metric(_action: str) -> None:
    """C3 度量：按清理路径计数（专用计数器 GOVERNANCE_PROPAGATED 见 core/metrics.py，execute 里按 reason 计）。"""
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
