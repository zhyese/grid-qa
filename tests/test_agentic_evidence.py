"""Agentic 只读证据：真实 sqlite 来源与权限，检索边界可替换。"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import update

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.kg_triple import KgTriple
from app.models.knowledge_governance import KnowledgeDocumentMetadata
from app.models.ops_workbench import TelemetryPoint

pytestmark = pytest.mark.asyncio


def reader(db, **kwargs):
    from app.services.agentic_evidence_service import EvidenceReader
    return EvidenceReader(db, tenant="tenant-a", role=kwargs.get("role", "operator"),
                          dept=kwargs.get("dept", "operations"))


async def seed_doc(db, *, doc_id="doc-a", tenant="tenant-a", dept="operations",
                   roles="operator", doc_type="运维手册", text="油温不得超过85℃。"):
    db.add(Document(id=doc_id, doc_name=f"{doc_id}.pdf", minio_object=f"{doc_id}.pdf",
                    tenant_id=tenant, dept=dept, allowed_roles=roles,
                    status="vectorized", doc_type=doc_type))
    db.add(Chunk(id=f"{doc_id}-chunk", doc_id=doc_id, chunk_idx=0, content=text,
                 page_num=3, section_path="2 > 油温", metadata_complete=True))
    await db.commit()


async def test_open_reads_real_source_with_location(test_db):
    # 丢失原文/章节/页码或默认伪造版本将破坏这个断言。
    await seed_doc(test_db)
    test_db.add(KnowledgeDocumentMetadata(doc_id="doc-a", tenant_id="tenant-a",
                                         version_label="2026-v2", version_status="active"))
    await test_db.commit()
    result = await reader(test_db).execute("open_document", {"doc_id": "doc-a", "chunk_idx": 0})
    item = result["evidence"][0]
    assert (item["text"], item["docName"], item["chunkId"], item["chunkIdx"],
            item["pageNum"], item["sectionPath"], item["version"]) == (
        "油温不得超过85℃。", "doc-a.pdf", "doc-a-chunk", 0, 3, "2 > 油温", "2026-v2")
    assert item["sourceHash"] and item["key"]


@pytest.mark.parametrize("tenant,dept,roles,user_dept,user_role", [
    ("tenant-b", "operations", "operator", "operations", "admin"),
    ("tenant-a", "secret", "operator", "operations", "operator"),
    ("tenant-a", "operations", "operator", "", "operator"),
    ("tenant-a", "operations", "manager", "operations", "operator"),
    ("tenant-a", "", "manager", "operations", "operator"),
])
async def test_open_denies_tenant_department_role_bypass(
        test_db, tenant, dept, roles, user_dept, user_role):
    await seed_doc(test_db, tenant=tenant, dept=dept, roles=roles)
    result = await reader(test_db, dept=user_dept, role=user_role).execute(
        "open_document", {"doc_id": "doc-a", "chunk_idx": 0})
    assert result["evidence"] == []


@pytest.mark.parametrize("args", [
    {"doc_id": "doc-a", "chunk_idx": 0, "tenant": "tenant-b"},
    {"doc_id": "doc-a", "chunk_idx": 0, "role": "admin"},
    {"doc_id": "doc-a", "chunk_idx": 0, "dept": "secret"},
    {"doc_id": "doc-a", "chunk_idx": True},
    {"doc_id": "doc-a", "chunk_idx": -1},
])
async def test_execute_rejects_extra_or_invalid_arguments(test_db, args):
    await seed_doc(test_db)
    result = await reader(test_db).execute("open_document", args)
    assert result["evidence"] == []
    assert result["note"]
    assert result["error"] is True


async def test_unknown_tools_never_read_or_write(test_db):
    result = await reader(test_db).execute("create_ticket", {"task": "执行停电"})
    assert result["evidence"] == []
    assert result["note"]
    assert result["error"] is True


async def test_search_discards_untrusted_payload_and_unknown_sources(test_db, monkeypatch):
    from app.services import retrieval_service
    await seed_doc(test_db, text="原始规程" * 1000)
    await seed_doc(test_db, doc_id="foreign", tenant="tenant-b")

    async def hits(db, query, topk, **kwargs):
        if kwargs.get("tenant") != "tenant-a" or kwargs.get("user_role") != "operator":
            return []
        return [
            {"docId": "doc-a", "chunkIdx": 0, "chunkId": "doc-a-chunk", "chunk": "伪造摘要", "docName": "伪造名"},
            {"docId": "foreign", "chunkIdx": 0, "chunk": "其他租户"},
            {"docId": "missing", "chunkIdx": 0, "chunk": "来源不存在"},
            {"docId": "doc-a", "chunkIdx": 0, "chunkId": "foreign-chunk", "chunk": "错配"},
        ]

    monkeypatch.setattr(retrieval_service, "mixed_search", hits)
    result = await reader(test_db).execute("search_documents", {"query": "油温"})
    assert len(result["evidence"]) == 1
    item = result["evidence"][0]
    assert item["text"] == "原始规程" * 450
    assert item["docName"] == "doc-a.pdf"


async def test_cases_only_accept_real_case_documents(test_db, monkeypatch):
    from app.services import retrieval_service
    await seed_doc(test_db)
    await seed_doc(test_db, doc_id="case-a", doc_type="故障案例")

    async def hits(db, query, topk, **kwargs):
        return [{"docId": d, "chunkIdx": 0} for d in ("doc-a", "case-a")]

    monkeypatch.setattr(retrieval_service, "mixed_search", hits)
    result = await reader(test_db).execute("search_cases", {"query": "油温"})
    assert [e["docId"] for e in result["evidence"]] == ["case-a"]


@pytest.mark.parametrize("change", ["content", "acl", "withdraw", "version", "location"])
async def test_revalidation_drops_changed_sources(test_db, change):
    await seed_doc(test_db)
    test_db.add(KnowledgeDocumentMetadata(doc_id="doc-a", tenant_id="tenant-a", version_status="active"))
    await test_db.commit()
    service = reader(test_db)
    evidence = (await service.execute("open_document", {"doc_id": "doc-a", "chunk_idx": 0}))["evidence"]
    if change == "content":
        stmt = update(Chunk).values(content="已修改阈值")
    elif change == "acl":
        stmt = update(Document).values(allowed_roles="manager")
    elif change == "withdraw":
        stmt = update(KnowledgeDocumentMetadata).values(version_status="withdrawn")
    elif change == "version":
        stmt = update(KnowledgeDocumentMetadata).values(version_label="new-version")
    else:
        stmt = update(Chunk).values(page_num=9)
    await test_db.execute(stmt.execution_options(synchronize_session=False))
    await test_db.commit()
    assert await service.revalidate(evidence) == []


async def test_revalidation_preserves_controller_fields_and_truncated_source(test_db):
    await seed_doc(test_db, text="原" * 5000)
    service = reader(test_db)
    item = (await service.execute("open_document", {"doc_id": "doc-a", "chunk_idx": 0}))["evidence"][0]
    assert len(item["text"]) == 3600
    item["evidenceId"] = "E7"
    item["selectedBy"] = "search"
    validated = await service.revalidate([item])
    assert validated == [item]
    # 片段外的原文变化同样必须失效。
    await test_db.execute(update(Chunk).values(content="原" * 4999 + "变"))
    await test_db.commit()
    assert await service.revalidate([item]) == []


async def test_governance_failure_is_fail_closed(test_db, monkeypatch):
    from app.services import knowledge_governance_service
    await seed_doc(test_db)

    async def failure(*args, **kwargs):
        raise RuntimeError("governance unavailable")

    monkeypatch.setattr(knowledge_governance_service, "blocked_document_ids", failure)
    result = await reader(test_db).execute("open_document", {"doc_id": "doc-a", "chunk_idx": 0})
    assert result["evidence"] == []
    assert result["degraded"] is True
    assert "governance unavailable" not in result["note"]


async def test_graph_requires_authorized_real_source_and_rechecks_triple(test_db):
    await seed_doc(test_db)
    await seed_doc(test_db, doc_id="foreign", tenant="tenant-b")
    for triple_id, doc_id in [("valid", "doc-a"), ("foreign", "foreign"), ("orphan", "")]:
        test_db.add(KgTriple(id=triple_id, doc_id=doc_id, doc_name="不可信缓存名",
                             subject="变压器", relation="故障", object="油温升高"))
    await test_db.commit()
    service = reader(test_db)
    items = (await service.execute("query_graph", {"entity": "变压器"}))["evidence"]
    assert len(items) == 1
    assert (items[0]["tripleId"], items[0]["docName"], items[0]["text"]) == (
        "valid", "doc-a.pdf", "变压器 — 故障 → 油温升高")
    await test_db.execute(update(KgTriple).where(KgTriple.id == "valid").values(object="已修正"))
    await test_db.commit()
    assert await service.revalidate(items) == []


async def test_graph_revalidation_rejects_rewritten_source_document(test_db):
    # 三元组暂未重抽取时，原文更新不能继续把旧关系当成当前证据。
    await seed_doc(test_db)
    test_db.add(KgTriple(id="valid", doc_id="doc-a", subject="变压器",
                         relation="故障", object="油温升高"))
    await test_db.commit()
    service = reader(test_db)
    items = (await service.execute("query_graph", {"entity": "变压器"}))["evidence"]
    await test_db.execute(update(Chunk).values(content="此设备不适用旧处置规则。"))
    await test_db.commit()
    assert await service.revalidate(items) == []


async def test_empty_telemetry_is_a_gap_not_evidence(test_db):
    result = await reader(test_db).execute("query_telemetry", {"device_id": "不存在设备"})
    assert result["evidence"] == []
    assert result["note"]
    assert not result.get("degraded")


async def test_telemetry_preserves_absolute_window_unit_and_rechecks_points(test_db):
    now = datetime.now()
    test_db.add_all([
        TelemetryPoint(id=f"p{i}", tenant_id="tenant-a", source="csv", device_id="主变1",
                       metric="油温", unit="℃", value=value, quality="good", imported_by="tester",
                       observed_at=now - timedelta(hours=2-i))
        for i, value in enumerate([60.0, 70.0])
    ])
    test_db.add(TelemetryPoint(id="foreign-point", tenant_id="tenant-b", source="csv",
                              device_id="主变1", metric="油温", unit="℃", value=999,
                              quality="good", imported_by="tester", observed_at=now-timedelta(hours=1)))
    await test_db.commit()
    service = reader(test_db)
    items = (await service.execute("query_telemetry", {"device_id": "主变1", "metric": "油温", "hours": 24}))["evidence"]
    assert len(items) == 1
    item = items[0]
    assert item["unit"] == "℃" and item["deviceId"] == "主变1"
    assert datetime.fromisoformat(item["start"]) < datetime.fromisoformat(item["end"])
    assert "70" in item["text"] and "999" not in item["text"]
    # 单位不能只在 JSON 的独立字段中；每个可引用数值都携带正确单位。
    assert all(fragment in item["text"] for fragment in (
        "最小60℃", "最大70℃", "均值65℃", "首值60℃", "末值70℃", "变化10℃", "样本2个"))
    assert "2℃" not in item["text"]
    assert await service.revalidate(items) == items
    await test_db.execute(update(TelemetryPoint).where(TelemetryPoint.id == "p1").values(value=75))
    await test_db.commit()
    assert await service.revalidate(items) == []


async def test_telemetry_revalidation_does_not_trust_mutated_evidence_stats(test_db):
    test_db.add(TelemetryPoint(id="p1", tenant_id="tenant-a", source="csv", device_id="主变1",
                              metric="油温", unit="℃", value=60, quality="good", imported_by="tester",
                              observed_at=datetime.now()-timedelta(hours=1)))
    await test_db.commit()
    service = reader(test_db)
    items = (await service.execute("query_telemetry", {"device_id": "主变1"}))["evidence"]
    items[0]["stats"]["max"] = 999
    validated = await service.revalidate(items)
    assert not validated or validated[0]["stats"]["max"] == 60


async def test_telemetry_unit_text_preserves_source_numeric_precision(test_db):
    test_db.add(TelemetryPoint(id="p1", tenant_id="tenant-a", source="csv", device_id="主变1",
                              metric="油温", unit="℃", value=85.1234567, quality="good",
                              imported_by="tester", observed_at=datetime.now()-timedelta(hours=1)))
    await test_db.commit()
    items = (await reader(test_db).execute("query_telemetry", {"device_id": "主变1"}))["evidence"]
    assert "最大85.1234567℃" in items[0]["text"]
