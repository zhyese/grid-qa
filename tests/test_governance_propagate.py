"""数据飞轮·A3+A4 治理联动订阅单测。

governance.doc_blocked → handler 联动清理 Milvus(双 collection) + Neo4j(按 doc_id) +
qa_cache(MySQL retrieval_sources 含 docId 行 + Redis qa:* 同 key)。
开关：GOVERNANCE_PROPAGATE_ENABLE（默认关=仅过滤现状）。
"""
import asyncio
import json


def _run(coro):
    return asyncio.run(coro)


def _enable(monkeypatch):
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "GOVERNANCE_PROPAGATE_ENABLE", True)


def test_a3_handler_deletes_milvus_vectors(monkeypatch):
    """A3：handler → milvus_client.delete_by_doc 被调（双 collection 联动删）。"""
    import app.services.governance_propagate_service as gps
    _enable(monkeypatch)

    calls = []

    def fake_delete_by_doc(doc_id):
        calls.append(("milvus", doc_id))

    monkeypatch.setattr(gps.milvus_client, "delete_by_doc", fake_delete_by_doc)
    # Neo4j + qa_cache 也走分支，mock 之避免真依赖
    async def fake_neo4j(doc_id):
        calls.append(("neo4j", doc_id))
    monkeypatch.setattr(gps, "_purge_neo4j_for_doc", fake_neo4j)
    monkeypatch.setattr(gps, "_invalidate_qa_cache_for_doc", fake_neo4j)

    _run(gps.propagate_handler(
        "eid", "governance", "doc_blocked",
        {"doc_id": "d-x", "reason": "withdrawn"}, "default"))

    assert ("milvus", "d-x") in calls


def test_a4_handler_purges_neo4j_and_qa_cache(monkeypatch):
    """A4：handler → Neo4j 按 doc_id 清 + qa_cache 扫 retrievalSource 含 docId 行删。"""
    import app.services.governance_propagate_service as gps
    _enable(monkeypatch)

    calls = []

    monkeypatch.setattr(gps.milvus_client, "delete_by_doc",
                        lambda did: calls.append(("milvus", did)))
    async def fake_neo4j(doc_id):
        calls.append(("neo4j", doc_id))
    monkeypatch.setattr(gps, "_purge_neo4j_for_doc", fake_neo4j)
    async def fake_cache(doc_id):
        calls.append(("qa_cache", doc_id))
    monkeypatch.setattr(gps, "_invalidate_qa_cache_for_doc", fake_cache)

    _run(gps.propagate_handler(
        "eid", "governance", "doc_blocked",
        {"doc_id": "d-y", "reason": "superseded"}, "default"))

    assert ("neo4j", "d-y") in calls
    assert ("qa_cache", "d-y") in calls


def test_a4_qa_cache_scan_deletes_rows_with_docid(monkeypatch):
    """A4：扫 qa_cache 表，retrieval_sources / answer JSON 含 docId 的行被删。"""
    import app.services.governance_propagate_service as gps

    # Fake session：execute 返回行列表； captured delete 语句
    class _Row:
        def __init__(self, id, cache_key, answer, retrieval_sources):
            self.id = id
            self.cache_key = cache_key
            self.answer = answer
            self.retrieval_sources = retrieval_sources

    class _R:
        def __init__(self, rows):
            self._rows = rows
        def all(self):
            return self._rows

    class _FakeDB:
        def __init__(self, rows):
            self.rows = rows
            self.deleted_ids = []
            self.commits = 0
        async def execute(self, stmt, *args, **kwargs):
            stmt_str = str(stmt).lower()
            if "delete" in stmt_str:
                class _DR:
                    def __init__(self, rowcount): self.rowcount = rowcount
                return _DR(0)
            # select：返回所有候选行（service 端按 LIKE 含 docId 已过滤）
            return _R(self.rows)
        async def commit(self):
            self.commits += 1

    rows = [
        _Row(1, "qa:default:m:q1:cv1",
             json.dumps({"retrievalSource": [{"docId": "d-y"}]}),
             json.dumps([{"docId": "d-y"}])),
        _Row(2, "qa:default:m:q2:cv1",
             json.dumps({"retrievalSource": [{"docId": "other"}]}),
             json.dumps([{"docId": "other"}])),
    ]
    db = _FakeDB(rows)

    deleted_redis = []

    class _FakeRedis:
        async def delete(self, key):
            deleted_redis.append(key)

    monkeypatch.setattr(gps.redis_client, "get_redis", lambda: _FakeRedis())

    # 用真 _FakeDB 走独立 session 路径：patch AsyncSessionLocal
    monkeypatch.setattr(gps, "AsyncSessionLocal", lambda: _CtxDB(db))

    _run(gps._invalidate_qa_cache_for_doc("d-y"))

    # 至少 Redis key 失效被调一次（行 1 含 d-y）
    assert any("q1" in k for k in deleted_redis)


class _CtxDB:
    """async ctx mgr 包装 _FakeDB。"""
    def __init__(self, db):
        self.db = db
    async def __aenter__(self):
        return self.db
    async def __aexit__(self, *a):
        return False


def test_a3_disabled_no_op(monkeypatch):
    """GOVERNANCE_PROPAGATE_ENABLE=False（默认）→ handler 直接 return，不碰 milvus。"""
    import app.services.governance_propagate_service as gps
    import app.config as cfg

    monkeypatch.setattr(cfg.settings, "GOVERNANCE_PROPAGATE_ENABLE", False)
    called = []
    monkeypatch.setattr(gps.milvus_client, "delete_by_doc",
                        lambda did: called.append(did))

    _run(gps.propagate_handler(
        "eid", "governance", "doc_blocked",
        {"doc_id": "d-z", "reason": "withdrawn"}, "default"))
    assert called == []


def test_a3_handler_exception_does_not_throw(monkeypatch):
    """milvus 抛异常 → degraded 不传播（订阅者异常不阻塞总线）。"""
    import app.services.governance_propagate_service as gps
    import app.config as cfg

    monkeypatch.setattr(cfg.settings, "GOVERNANCE_PROPAGATE_ENABLE", True)

    def boom(doc_id):
        raise RuntimeError("milvus down")
    monkeypatch.setattr(gps.milvus_client, "delete_by_doc", boom)
    async def fake_neo4j(doc_id): return None
    async def fake_cache(doc_id): return None
    monkeypatch.setattr(gps, "_purge_neo4j_for_doc", fake_neo4j)
    monkeypatch.setattr(gps, "_invalidate_qa_cache_for_doc", fake_cache)

    # 不应抛
    _run(gps.propagate_handler(
        "eid", "governance", "doc_blocked",
        {"doc_id": "d-bOOM", "reason": "expired"}, "default"))
