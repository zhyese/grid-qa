"""历史证据快照：真实 sqlite 持久化、权限撤回和接口边界。"""
import importlib
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, select, update

from app.core.response import BizError
from app.models.chunk import Chunk
from app.models.conversation import Conversation, Message
from app.models.document import Document
from app.models.kg_triple import KgTriple
from app.models.user import User
from app.services.agentic_evidence_service import EvidenceReader

pytestmark = pytest.mark.asyncio


def service():
    name = "app.services.agentic_history_service"
    assert importlib.util.find_spec(name) is not None, "缺少可持久化的 Agentic 历史服务"
    return importlib.import_module(name)


async def seed(db):
    db.add(Conversation(id="conv", username="owner", title="历史"))
    await db.commit()


async def evidence(db, doc_id="doc-a", *, graph=False):
    db.add(Document(id=doc_id, doc_name=f"{doc_id}.pdf", minio_object=doc_id,
                    tenant_id="one", dept="检修", allowed_roles="operator", status="vectorized"))
    db.add(Chunk(id=f"{doc_id}-chunk", doc_id=doc_id, chunk_idx=0,
                 content=f"{doc_id}保密证据85℃", page_num=3))
    if graph:
        db.add(KgTriple(id=f"{doc_id}-graph", doc_id=doc_id, subject="变压器",
                        relation="涉及", object="保密故障"))
    await db.commit()
    reader = EvidenceReader(db, tenant="one", role="operator", dept="检修")
    result = await reader.execute("query_graph" if graph else "open_document",
                                  {"entity": "变压器"} if graph else {"doc_id": doc_id, "chunk_idx": 0})
    return result["evidence"][0]


def result(items=()):
    pack = [{**item, "evidenceId": f"E{i + 1}"} for i, item in enumerate(items)]
    return {
        "answer": "保密结论[1]", "status": "completed", "stopReason": "sufficient",
        "sources": [{"docId": e.get("docId", ""), "kind": e["kind"],
                     "docName": e.get("docName", "遥测"), "evidenceId": e["evidenceId"],
                     "chunk": e["text"]} for e in pack],
        "evidencePack": pack, "missingEvidence": [], "conflicts": [],
        "rounds": 2, "toolCalls": 2, "toolsUsed": ["open_document"],
        "confidence": "medium", "degraded": False,
        "verification": {"citationIdsChecked": True, "semanticEntailmentVerified": False},
        "steps": [{"iter": 1, "phase": "retrieve", "tool": "open_document",
                   "result": "取得1条证据", "error": False, "thought": "不得保存的私密推理",
                   "args": {"doc_id": "doc-a"}, "raw": "不公开的响应"}],
    }


async def save(db, payload=None, **kwargs):
    await service().save_answer(db, "conv", "问题", payload or result(),
                                username=kwargs.get("username", "owner"),
                                tenant=kwargs.get("tenant", "one"))


async def read(db, **kwargs):
    return await service().history(db, "conv", username=kwargs.get("username", "owner"),
                                   tenant=kwargs.get("tenant", "one"),
                                   role="operator", dept="检修", limit=kwargs.get("limit", 100))


async def test_round_trip_restores_snapshot_and_public_steps(test_db):
    # 若只存普通文本、丢引用/终态、或把模型私密推理混入快照，此测试失败。
    await seed(test_db)
    payload = result([await evidence(test_db)])
    await save(test_db, payload)
    messages = await read(test_db)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    answer = messages[1]
    assert answer["content"] == "保密结论[1]"
    assert answer["agenticRag"] is True and answer["agentMode"] is True
    assert answer["historySnapshot"] is True
    assert answer["confidence"] == "medium" and answer["degraded"] is False
    assert answer["sources"][0]["citationIndex"] == 1
    assert answer["evidencePack"][0]["evidenceId"] == "E1"
    assert answer["agenticResult"]["stopReason"] == "sufficient"
    assert answer["agenticResult"]["rounds"] == 2
    assert answer["agentSteps"][0]["result"] == "取得1条证据"
    from app.models.agentic_answer import AgenticAnswer
    snapshot = (await test_db.execute(select(AgenticAnswer))).scalar_one()
    assert snapshot.message_id == answer["id"]
    assert (snapshot.tenant_id, snapshot.username) == ("one", "owner")
    stored = json.dumps(snapshot.payload, ensure_ascii=False)
    assert "不得保存的私密推理" not in stored and "不公开的响应" not in stored
    assert "citationIndex" not in payload["sources"][0]  # 不修改 controller 的共享结果


async def test_snapshot_failure_rolls_back_both_messages(test_db):
    # 若先提交user/assistant再保存JSON，注入snapshot插入失败后会留下半套消息。
    await seed(test_db)
    history = service()

    def reject_snapshot(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("INSERT INTO AGENTIC_ANSWERS"):
            raise RuntimeError("snapshot storage unavailable")

    event.listen(test_db.bind.sync_engine, "before_cursor_execute", reject_snapshot)
    try:
        with pytest.raises(RuntimeError, match="snapshot storage unavailable"):
            await history.save_answer(test_db, "conv", "问题", result(), username="owner", tenant="one")
    finally:
        event.remove(test_db.bind.sync_engine, "before_cursor_execute", reject_snapshot)
    assert (await test_db.execute(select(Message))).scalars().all() == []


@pytest.mark.parametrize("blocked", ["owner", "deleted", "tenant", "empty_tenant"])
async def test_history_never_leaks_unowned_deleted_or_cross_tenant_answers(test_db, blocked):
    await seed(test_db)
    await save(test_db)
    kwargs = {}
    if blocked == "owner":
        kwargs["username"] = "intruder"
    elif blocked == "deleted":
        await test_db.execute(update(Conversation).values(is_deleted=True))
        await test_db.commit()
    else:
        kwargs["tenant"] = "two" if blocked == "tenant" else ""
    assert await read(test_db, **kwargs) == []


async def test_save_rejects_other_owner_and_existing_cross_tenant_snapshot(test_db):
    await seed(test_db)
    with pytest.raises(BizError):
        await save(test_db, username="intruder")
    await save(test_db)
    with pytest.raises(BizError):
        await save(test_db, tenant="two")
    assert len((await test_db.execute(select(Message))).scalars().all()) == 2


async def test_ordinary_history_keeps_shape_and_omits_deleted_messages(test_db):
    await seed(test_db)
    for index in range(3):
        test_db.add(Message(id=f"m{index}", conversation_id="conv", role="user",
                            content=f"普通{index}", created_at=datetime(2026, 1, 1) + timedelta(seconds=index),
                            is_deleted=index == 1))
    await test_db.commit()
    assert await read(test_db, limit=1) == [{"id": "m2", "role": "user", "content": "普通2"}]


async def test_message_timestamp_uses_database_clock_not_container_local_clock(test_db, monkeypatch):
    from sqlalchemy import func
    await seed(test_db)
    class WrongLocalClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2000,1,1)
    monkeypatch.setattr(service(), 'datetime', WrongLocalClock)
    server_now=(await test_db.execute(select(func.now()))).scalar_one()
    await save(test_db)
    rows=(await test_db.execute(select(Message))).scalars().all()
    assert all(abs((row.created_at-server_now).total_seconds())<3 for row in rows)


@pytest.mark.parametrize("change", ["acl", "content", "graph"])
async def test_changed_sources_hide_old_answer_without_renumbering_remaining_citations(test_db, change):
    # 撤权后不能靠历史消息重读原文；过滤[1]后原[2]必须仍对应E2。
    await seed(test_db)
    first = await evidence(test_db, graph=change == "graph")
    second = await evidence(test_db, "doc-b")
    payload = result([first, second])
    payload["steps"][0]["result"] = "doc-a保密证据85℃"
    payload["conflicts"] = ["doc-a保密证据85℃"]
    await save(test_db, payload)
    if change == "acl":
        await test_db.execute(update(Document).where(Document.id == "doc-a").values(allowed_roles="admin"))
    elif change == "content":
        await test_db.execute(update(Chunk).where(Chunk.doc_id == "doc-a").values(content="已更新"))
    else:
        await test_db.execute(update(KgTriple).where(KgTriple.doc_id == "doc-a").values(object="关系已变"))
    await test_db.commit()
    answer = (await read(test_db))[-1]
    assert [(s["citationIndex"], s["evidenceId"]) for s in answer["sources"]] == [(2, "E2")]
    assert [e["evidenceId"] for e in answer["evidencePack"]] == ["E2"]
    assert "保密结论" not in answer["content"]
    assert "doc-a保密证据85℃" not in json.dumps(answer, ensure_ascii=False)
    assert answer["agenticResult"]["stopReason"] == "source_changed"
    assert answer["agenticResult"]["status"] != "completed"
    assert answer["historyWarnings"] and answer["historySnapshot"]


async def test_historical_telemetry_is_not_requeried_as_current_data(test_db, monkeypatch):
    await seed(test_db)
    await save(test_db, result([{"kind": "telemetry", "key": "telemetry:old", "sourceHash": "hash",
                                "text": "旧遥测85℃", "deviceId": "T1", "start": "2025-01-01T00:00:00",
                                "end": "2025-01-02T00:00:00"}]))
    from app.services import telemetry_history_service

    async def no_current_window(*args, **kwargs):
        pytest.fail("历史读取不得按当前时间重新查询遥测")

    monkeypatch.setattr(telemetry_history_service, "series", no_current_window)
    answer = (await read(test_db))[-1]
    assert answer["sources"] == [] and answer["evidencePack"] == []
    assert "旧遥测85℃" not in json.dumps(answer, ensure_ascii=False)
    assert any("非实时" in warning for warning in answer["historyWarnings"])


async def test_history_route_enforces_owner_and_returns_structured_snapshot(test_db):
    # 路由若仍直接get_messages，其他用户能读且合法用户拿不到快照。
    await seed(test_db)
    await save(test_db)
    from app.routers.qa import history
    denied = await history("conv", db=test_db, user=User(username="intruder", tenant_id="one"))
    assert denied.data == []
    allowed = await history("conv", db=test_db,
                            user=User(username="owner", tenant_id="one", role="operator", dept="检修"))
    assert allowed.data[-1]["historySnapshot"] is True
