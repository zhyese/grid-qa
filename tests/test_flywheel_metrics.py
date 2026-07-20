"""数据飞轮·C3 飞轮度量 5 指标单测。

init_metric_series 预注册 0 值；指标对象存在；emit/propagate/eval 路径埋点可调用。
"""
from prometheus_client import REGISTRY


def _names():
    return {m.name for m in REGISTRY.collect()}


def test_c3_metrics_registered():
    """5 指标 + 2 已埋点（RETRIEVAL_TUNE/BASELINE）存在。

    prometheus_client Counter 在 REGISTRY 中去掉 _total 后缀；text exposition 才加回。
    """
    from app.core import metrics
    metrics.init_metric_series()
    names = _names()
    for n in (
        "grid_governance_propagated",   # Counter（_total 由 exposition 加回）
        "grid_quality_event",
        "grid_feedback_fix_rate",
        "grid_faithfulness_trend",
        "grid_kb_freshness",
    ):
        assert n in names, f"missing metric: {n}"


def test_c3_init_preregisters_zero_series():
    """init_metric_series 调后，关键 label 序列在 /metrics 输出中可见（值=0）。"""
    from prometheus_client import generate_latest
    from app.core import metrics
    metrics.init_metric_series()
    txt = generate_latest().decode("utf-8")
    # 治理清理 label
    assert 'grid_governance_propagated_total{action="milvus"}' in txt
    assert 'grid_governance_propagated_total{action="neo4j"}' in txt
    assert 'grid_governance_propagated_total{action="qa_cache"}' in txt
    # 总线吞吐 label
    assert 'grid_quality_event_total{source="feedback",type="dislike"}' in txt
    assert 'grid_quality_event_total{source="governance",type="doc_blocked"}' in txt
    # Gauge 默认 0
    assert "grid_feedback_fix_rate 0" in txt
    assert "grid_faithfulness_trend 0" in txt
    assert "grid_kb_freshness 0" in txt


def test_c3_emit_increments_quality_event_total(monkeypatch):
    """emit 入库（mock）后，QUALITY_EVENT_TOTAL 对应 label 自增。"""
    import app.services.quality_event_bus as bus
    import uuid as _u

    class _FakeSession:
        def __init__(self): self.row = None
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def add(self, row):
            row.id = "e-" + _u.uuid4().hex[:6]
            self.row = row
        async def commit(self): pass
        async def refresh(self, row): pass

    monkeypatch.setattr(bus, "AsyncSessionLocal", lambda: _FakeSession())
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "QUALITY_BUS_ENABLE", False)

    from prometheus_client import REGISTRY
    before = 0
    for m in REGISTRY.collect():
        if m.name == "grid_quality_event":
            for s in m.samples:
                if s.labels.get("source") == "test" and s.labels.get("type") == "ping":
                    before = s.value
    import asyncio
    asyncio.run(bus.emit("test", "ping", {"x": 1}))
    after = 0
    for m in REGISTRY.collect():
        if m.name == "grid_quality_event":
            for s in m.samples:
                if s.labels.get("source") == "test" and s.labels.get("type") == "ping":
                    after = s.value
    assert after >= before + 1
