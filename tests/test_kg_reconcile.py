"""KG 对账任务单测：mock MySQL/Neo4j 双端计数，验证漂移分类 + 指标 + WARN 日志。"""
import pytest

from app.services import kg_reconcile_service as svc


@pytest.mark.asyncio
async def test_reconcile_pass_when_equal(monkeypatch, caplog):
    async def fake_mysql():
        return 100

    async def fake_neo4j():
        return 100

    monkeypatch.setattr(svc, "_mysql_distinct", fake_mysql)
    monkeypatch.setattr("app.clients.neo4j_client.count_rel_edges", fake_neo4j)
    with caplog.at_level("INFO", logger="app"):
        r = await svc.reconcile_once()
    assert r == {"mysql": 100, "neo4j": 100, "drift": 0, "warn": False}
    assert any("对账通过" in x.message for x in caplog.records)


@pytest.mark.asyncio
async def test_reconcile_warn_on_drift(monkeypatch, caplog):
    """漂移超过阈值(>5) → warn=True + WARN 日志带修复指引。"""
    async def fake_mysql():
        return 11052

    async def fake_neo4j():
        return 61  # 2026-09-17 实锤事故的数字

    monkeypatch.setattr(svc, "_mysql_distinct", fake_mysql)
    monkeypatch.setattr("app.clients.neo4j_client.count_rel_edges", fake_neo4j)
    with caplog.at_level("WARNING", logger="app"):
        r = await svc.reconcile_once()
    assert r["drift"] == 10991 and r["warn"] is True
    assert any("漂移" in x.message and "rebuild" in x.message for x in caplog.records)


@pytest.mark.asyncio
async def test_reconcile_metric_set(monkeypatch):
    from app.core.metrics import KG_DRIFT_TRIPLES, KG_RECONCILE

    async def fake_mysql():
        return 50

    async def fake_neo4j():
        return 47

    monkeypatch.setattr(svc, "_mysql_distinct", fake_mysql)
    monkeypatch.setattr("app.clients.neo4j_client.count_rel_edges", fake_neo4j)
    await svc.reconcile_once()
    assert KG_DRIFT_TRIPLES._value.get() == 3.0
    assert KG_RECONCILE.labels(store="mysql_distinct")._value.get() == 50.0
    assert KG_RECONCILE.labels(store="neo4j_edges")._value.get() == 47.0


@pytest.mark.asyncio
async def test_reconcile_loop_degrades_not_raises(monkeypatch):
    """对账抛异常 → degraded() 吞掉不中断 loop。"""
    called = []

    async def boom():
        called.append(1)
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(svc, "reconcile_once", boom)
    # 跑一轮即退出（interval 0.1h=360s，靠 cancel 打断 sleep）
    t = __import__("asyncio").create_task(svc.reconcile_loop(0.1))
    await __import__("asyncio").sleep(0.2)
    t.cancel()
    with pytest.raises(__import__("asyncio").CancelledError):
        await t
    assert called, "loop 应至少执行一轮"
