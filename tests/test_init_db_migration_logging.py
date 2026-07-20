"""init_db _ensure_columns 错误可见性测试（防 schema drift 静默吞）。

原 bare except 静默吞所有异常导致 chunks.table_header 加列失败后 SELECT 1054 长期不可见。
现版区分 1060（幂等跳过）与其他异常（degraded 记录）。
"""
import asyncio


def _run(coro):
    return asyncio.run(coro)


def test_ensure_columns_silent_on_duplicate_column(monkeypatch):
    """MySQL 1060（列已存在）→ 静默跳过，不算 drift。"""
    import app.db.init_db as idb

    class _FakeConn:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, stmt):
            # 模拟 MySQL 1060 错误
            raise Exception("(pymysql.err.OperationalError) (1060, \"Duplicate column name 'table_header'\")")

    class _FakeEngine:
        def begin(self):
            return _FakeConn()

    monkeypatch.setattr(idb, "engine", _FakeEngine())

    degraded_calls = []
    def fake_degraded(tag, e, ctx=None):
        degraded_calls.append((tag, ctx))
    # degraded 在函数内 import，patch 源模块
    import app.core.obs as obs
    monkeypatch.setattr(obs, "degraded", fake_degraded)

    _run(idb._ensure_columns())

    # 1060 不应触发 degraded（幂等跳过）
    assert all("column_migration" != tag for tag, _ in degraded_calls)


def test_ensure_columns_logs_non_duplicate_errors(monkeypatch):
    """非 1060 异常（权限/连接/锁）→ degraded 记录，让 drift 可见。"""
    import app.db.init_db as idb

    class _FakeConn:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, stmt):
            # 模拟权限错误（不是 1060）
            raise Exception("(pymysql.err.OperationalError) (1142, \"ALTER command denied\"")

    class _FakeEngine:
        def begin(self):
            return _FakeConn()

    monkeypatch.setattr(idb, "engine", _FakeEngine())

    degraded_calls = []
    def fake_degraded(tag, e, ctx=None):
        degraded_calls.append((tag, ctx))
    import app.core.obs as obs
    monkeypatch.setattr(obs, "degraded", fake_degraded)

    _run(idb._ensure_columns())

    # 非 1060 异常应触发 degraded 让 drift 可见
    assert any(tag == "init_db_column_migration" for tag, _ in degraded_calls)
    # ctx 含表.列 信息便于排障
    assert any(ctx and "chunks.chunk_type" in ctx for _, ctx in degraded_calls)
