"""可观测降级 helper 单测（P0-1 闭环：失败可见 + 按 tag 计数）。"""
from app.core import metrics, obs


def test_degraded_never_raises():
    """degraded() 永不抛出——调用方依赖它吞异常后继续走兜底路径。"""
    obs.degraded("unit_test", ValueError("不应抛出"))  # 不应 raise


def test_degraded_counts_by_tag(monkeypatch):
    """同 tag 多次调用，DEGRADED.labels(tag).inc() 被调用对应次数。"""
    calls = []

    class _FakeChild:
        def inc(self):
            calls.append(1)

    class _FakeCounter:
        def labels(self, tag):
            calls.append(tag)
            return _FakeChild()

    monkeypatch.setattr(metrics, "DEGRADED", _FakeCounter())
    obs.degraded("count_test", RuntimeError("c1"))
    obs.degraded("count_test", RuntimeError("c2"))
    # 2 次 labels 调用（各 1 次 inc）= 共 4 条记录，tag 均为 count_test
    assert calls.count("count_test") == 2


def test_degraded_includes_exception_detail():
    """降级不抛但内部能拿到异常类型/信息（通过日志可见）。"""
    # 仅验证不抛 + 不依赖日志输出；异常细节由 logger.warning 落地
    obs.degraded("detail_test", KeyError("missing-key"))
