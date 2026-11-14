"""证据自动补全必须等待人工审核；启动注册不改变治理安全开关。"""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.evidence_gap import EvidenceGap
from app.models.qa_cache import QaCache
from app.models.quality_event import QualityEvent
from app.services import evidence_gap_service as svc


@pytest.fixture
def isolated_sessions(test_db, monkeypatch):
    """真实 SQL 状态转换，仅将业务 session 指向该用例的内存 sqlite。"""
    from app.services import persona_store, quality_event_bus

    sessions = async_sessionmaker(test_db.bind, expire_on_commit=False)
    for module in (svc, persona_store, quality_event_bus):
        monkeypatch.setattr(module, "AsyncSessionLocal", sessions)
    return sessions


@pytest.fixture
def external_stores(monkeypatch):
    """隔离 MinIO/Milvus/Redis；Document/Chunk/QaCache 的真实 SQL 写入不替换。"""
    from app.clients import minio_client, redis_client
    from app.services import document_service

    stores = {"objects": {}, "vectors": {}, "redis": {}}

    def put_object(name, data, length, content_type):
        assert len(data) == length
        assert content_type == "text/markdown"
        stores["objects"][name] = data

    async def vectorize(db, doc_id):
        doc = await db.get(Document, doc_id)
        chunks = (await db.execute(select(Chunk).where(Chunk.doc_id == doc_id))).scalars().all()
        stores["vectors"][doc_id] = [chunk.content for chunk in chunks]
        doc.status = "vectorized"
        await db.commit()

    async def cache_set(key, value, ttl):
        assert ttl > 0
        stores["redis"][key] = value

    monkeypatch.setattr(minio_client, "put_object", put_object)
    monkeypatch.setattr(document_service, "vectorize_document", vectorize)
    monkeypatch.setattr(redis_client, "cache_set_json", cache_set)
    return stores


def _agent_answer(monkeypatch, answer):
    from app.services import agent_runtime

    async def run_agent(db, persona, query, model_type):
        return agent_runtime.AgentResult(
            answer=answer, steps=[], iterations=1, degraded=False,
            degrade_reason=None, latency_ms=1, persona="evidence_gap", tools_used=[],
        )

    monkeypatch.setattr(agent_runtime, "run_agent", run_agent)


@pytest.mark.asyncio
async def test_scheduled_draft_waits_for_review_without_publishing(
    test_db, isolated_sessions, external_stores, monkeypatch,
):
    """自动确认回归会写正式文档/缓存，并把状态越过 ai_drafted。"""
    row = EvidenceGap(query="主变油温异常如何核实？", tenant="tenant-a")
    test_db.add(row)
    await test_db.commit()
    _agent_answer(monkeypatch, "AI 建议草稿，尚未经过审核。")

    stats = await svc.batch_deep_and_sync("tenant-a")

    await test_db.refresh(row)
    assert row.status == "ai_drafted"
    assert row.ai_draft == "AI 建议草稿，尚未经过审核。"
    assert row.final_answer == ""
    assert row.synced_doc_id == ""
    assert row.synced_cache == 0
    assert row.operator == ""
    assert row.handled_at is None
    assert stats == {"scanned": 1, "drafted": 1, "synced": 0, "failed": 0, "awaitingReview": 1}
    for model in (Document, Chunk, QaCache):
        assert (await test_db.execute(select(func.count()).select_from(model))).scalar_one() == 0
    assert external_stores == {"objects": {}, "vectors": {}, "redis": {}}

    # 第二轮不能改写已提交审核的草稿，更不能将其同步。
    _agent_answer(monkeypatch, "不应覆盖第一版草稿")
    again = await svc.batch_deep_and_sync("tenant-a")
    await test_db.refresh(row)
    assert again == {"scanned": 0, "drafted": 0, "synced": 0, "failed": 0, "awaitingReview": 0}
    assert row.ai_draft == "AI 建议草稿，尚未经过审核。"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["", "  \n", None])
async def test_empty_draft_stays_pending_for_next_retry(
    test_db, isolated_sessions, external_stores, monkeypatch, answer,
):
    """空结果不能提交 ai_drafted，否则后续定时批次永远不会重试。"""
    row = EvidenceGap(query="补全失败后应重试", tenant="tenant-a")
    test_db.add(row)
    await test_db.commit()
    _agent_answer(monkeypatch, answer)

    stats = await svc.batch_deep_and_sync("tenant-a")
    await test_db.refresh(row)
    assert row.status == "pending"
    assert row.ai_draft == ""
    assert stats["failed"] == 1
    assert stats["synced"] == 0

    _agent_answer(monkeypatch, "重试得到的有效草稿")
    retried = await svc.batch_deep_and_sync("tenant-a")
    await test_db.refresh(row)
    assert retried["drafted"] == 1
    assert row.status == "ai_drafted"
    assert row.ai_draft == "重试得到的有效草稿"


@pytest.mark.asyncio
async def test_scheduled_draft_respects_tenant_deletion_status_and_limit(
    test_db, isolated_sessions, external_stores, monkeypatch,
):
    now = datetime(2026, 1, 1)
    rows = [
        EvidenceGap(query="其他租户", tenant="tenant-b", ts=now),
        EvidenceGap(query="已删除", tenant="tenant-a", ts=now, is_deleted=1),
        EvidenceGap(query="待审核", tenant="tenant-a", ts=now, status="ai_drafted", ai_draft="已存在"),
        EvidenceGap(query="先处理", tenant="tenant-a", ts=now),
        EvidenceGap(query="下批处理", tenant="tenant-a", ts=now + timedelta(seconds=1)),
    ]
    test_db.add_all(rows)
    await test_db.commit()
    _agent_answer(monkeypatch, "新草稿")

    stats = await svc.batch_deep_and_sync("tenant-a", limit=1)
    for row in rows:
        await test_db.refresh(row)
    assert stats["scanned"] == 1
    assert [row.status for row in rows] == ["pending", "pending", "ai_drafted", "ai_drafted", "pending"]
    assert [row.ai_draft for row in rows] == ["", "", "已存在", "新草稿", ""]


@pytest.mark.asyncio
async def test_human_confirmation_publishes_reviewed_answer(
    test_db, isolated_sessions, external_stores, monkeypatch,
):
    import app.config as config
    from app.services import knowledge_change_service

    monkeypatch.setattr(config, "_gov_generation", 0)
    invalidated = []

    async def invalidate_after_vectorization(doc_id, *, tenant_id):
        # 此处替换跨存储失效边界，保留本地代际变更与真实 FAQ/cache SQL 路径。
        async with isolated_sessions() as db:
            doc = await db.get(Document, doc_id)
            assert doc.status == "vectorized"
            assert (await db.execute(select(func.count()).select_from(QaCache))).scalar_one() == 0
        assert external_stores["vectors"][doc_id] == ["经人工核实后的答案"]
        invalidated.append((doc_id, tenant_id))
        config.bump_gov_generation_inproc()

    monkeypatch.setattr(knowledge_change_service, "knowledge_changed", invalidate_after_vectorization)
    row = EvidenceGap(query="人工审核问题", tenant="tenant-a", status="ai_drafted", ai_draft="初稿")
    test_db.add(row)
    await test_db.commit()

    result = await svc.confirm_and_sync(row.id, "经人工核实后的答案", "reviewer")

    assert result["ok"] is True
    await test_db.refresh(row)
    assert row.status == "synced"
    assert row.final_answer == "经人工核实后的答案"
    assert row.operator == "reviewer"
    assert row.synced_doc_id == result["docId"]
    doc = await test_db.get(Document, result["docId"])
    assert doc.upload_user == "reviewer"
    assert doc.tenant_id == "tenant-a"
    cache = (await test_db.execute(select(QaCache))).scalar_one()
    assert cache.tenant_id == "tenant-a"
    assert cache.confidence == "high"
    assert invalidated == [(doc.id, "tenant-a")]
    assert config._gov_generation == 1
    # 新缓存必须使用失效后的代际，否则入库即变成不可命中的旧版本。
    assert cache.query_hash == QaCache.build_hash(None, row.query, "tenant-a")
    assert cache.cache_key.endswith(":" + config.citation_cache_version())
    assert cache.is_deleted == 0
    assert external_stores["vectors"][doc.id] == ["经人工核实后的答案"]
    assert list(external_stores["redis"]) == [cache.cache_key]


@pytest.mark.asyncio
@pytest.mark.parametrize("propagation_enabled, expected_reports", [(True, 1), (False, 0)])
async def test_startup_registers_governance_once_without_enabling_cleanup(
    test_db, isolated_sessions, monkeypatch, propagation_enabled, expected_reports,
):
    """启动遗漏注册会丢失治理事件；重复启动不能重复派发或越过安全开关。"""
    from app import main
    from app.services import governance_propagate_service as propagation, quality_event_bus as bus

    monkeypatch.setattr(main.settings, "QUALITY_BUS_ENABLE", True)
    monkeypatch.setattr(main.settings, "GOVERNANCE_PROPAGATE_ENABLE", propagation_enabled)
    monkeypatch.setattr(main.settings, "GOVERNANCE_PROPAGATE_DRY_RUN_ENABLE", True)

    async def candidates(doc_id):
        assert doc_id == "review-doc"
        return {"milvus": {}, "neo4jEdges": 0, "kgTriples": 0,
                "qaCacheRows": 0, "cacheKeyPreview": [], "totalEstimate": 0}

    monkeypatch.setattr(propagation, "build_cleanup_candidates", candidates)

    class StartupCheckpoint(Exception):
        pass

    async def stop_before_external_initialization():
        raise StartupCheckpoint()

    monkeypatch.setattr(main, "_wait_for_startup_dependencies", stop_before_external_initialization)
    bus.reset_subscribers()  # 已 import 的模块不能靠偶然 import 副作用通过测试。
    for _ in range(2):
        with pytest.raises(StartupCheckpoint):
            async with main.lifespan(main.app):
                pytest.fail("应在外部依赖初始化前结束隔离启动")

    assert main.settings.GOVERNANCE_PROPAGATE_ENABLE is propagation_enabled
    assert main.settings.GOVERNANCE_PROPAGATE_DRY_RUN_ENABLE is True
    event_id = await bus.emit("governance", "doc_blocked", {"doc_id": "review-doc"}, "tenant-a")
    assert event_id
    while bus._bg_tasks:
        await asyncio.gather(*tuple(bus._bg_tasks))
    reports = (await test_db.execute(select(QualityEvent).where(
        QualityEvent.type == "cleanup_dry_run",
    ))).scalars().all()
    assert len(reports) == expected_reports
    if reports:
        assert reports[0].tenant == "tenant-a"
        assert reports[0].payload["doc_id"] == "review-doc"


@pytest.fixture
def startup_without_external_services(monkeypatch):
    """执行真实启动顺序，在第一个后台任务前停止；只替换外部初始化边界。"""
    from app import main
    from app.clients import nacos_client, neo4j_client
    from app.core import otel_genai
    from app.db import init_db
    from app.mcp.registry import mcp_registry
    from app.providers.embedding import bge_embedding
    from app.services import config_service, embedding_service

    async def no_io(*args, **kwargs):
        return None

    async def no_overrides():
        return 0

    async def consistent_model():
        return {"status": "unchanged", "rebuildPending": False}

    monkeypatch.setattr(main, "_wait_for_startup_dependencies", no_io)
    monkeypatch.setattr(main, "_retry_startup_component", no_io)
    monkeypatch.setattr(nacos_client, "apply_overrides", no_overrides)
    monkeypatch.setattr(init_db, "init_db", no_io)
    monkeypatch.setattr(embedding_service, "ensure_model_consistency", consistent_model)
    monkeypatch.setattr(bge_embedding, "_get_model", lambda: None)
    monkeypatch.setattr(neo4j_client, "ensure_constraint", no_io)
    monkeypatch.setattr(otel_genai, "init_otel", lambda: None)
    monkeypatch.setattr(mcp_registry, "load_from_config", no_io)
    monkeypatch.setattr(mcp_registry, "server_count", lambda: 0)
    monkeypatch.setattr(config_service, "load_runtime", no_io)

    class BeforeBackgroundTasks(Exception):
        pass

    def stop_before_tasks():
        raise BeforeBackgroundTasks()

    monkeypatch.setattr(main, "_refresh_component_health_loop", stop_before_tasks)
    return main, BeforeBackgroundTasks


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted, expected, failed", [
    (b"17", 17, False), (b"2", 4, False), (None, 4, False),
    (b"-1", 4, True), (b"invalid", 4, True), (ConnectionError("redis unavailable"), 4, True),
])
async def test_startup_restores_cache_generation_before_background_tasks(
    startup_without_external_services, monkeypatch, persisted, expected, failed,
):
    """遗漏恢复会重新使用旧缓存键；坏值或 Redis 故障不能倒退代际或静默失败。"""
    import app.config as config
    from app.clients import redis_client
    from app.core import metrics

    main, checkpoint = startup_without_external_services
    monkeypatch.setattr(config, "_gov_generation", 4)
    before_version = config.citation_cache_version()
    degradation = metrics.DEGRADED.labels("governance_generation_restore")
    before_degraded = degradation._value.get()

    class RedisSnapshot:
        async def get(self, key):
            assert key == "qa:gov_gen"
            if isinstance(persisted, Exception):
                raise persisted
            return persisted

    monkeypatch.setattr(redis_client, "get_redis", lambda: RedisSnapshot())
    with pytest.raises(checkpoint):
        async with main.lifespan(main.app):
            pytest.fail("隔离启动不能开始后台任务")

    assert config._gov_generation == expected
    assert (config.citation_cache_version() != before_version) is (expected != 4)
    assert degradation._value.get() - before_degraded == int(failed)
