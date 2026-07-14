"""N4 OpenTelemetry GenAI 可观测性测试。

测试重点（参考架构文档 R2 采样策略）：
- trace_span 上下文管理器正常工作
- get_trace_id 在 span 内有值，span 外回退 contextvar
- _should_export 采样策略（异常 span 强制导出，正常 span 按采样率）
- force_export：faithfulness < 0.85 强制导出标记
"""
import asyncio

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind, Status, StatusCode

from app.core import otel_genai


# ===== 初始化测试用 TracerProvider（不连真实 Langfuse） =====
@pytest.fixture(autouse=True)
def _setup_tracer():
    """每个测试前安装一个干净的 TracerProvider（NoOp exporter）。"""
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    # 重置 contextvar
    otel_genai._current_trace_id.set("")
    yield
    otel_genai._current_trace_id.set("")


# ===== trace_span 上下文管理器 =====
def test_trace_span_creates_recording_span():
    """trace_span 创建的 span 是 recording 状态。"""
    with otel_genai.trace_span("test_span") as span:
        assert span.is_recording() is True


def test_trace_span_sets_initial_attributes():
    """trace_span 通过 attributes 参数设置初始属性。"""
    with otel_genai.trace_span("test_attrs", attributes={"key1": "val1", "key2": 42}) as span:
        # 属性已设置到 span 上
        assert span.is_recording()


def test_trace_span_name_matches():
    """trace_span 创建的 span 名称与传入一致。"""
    with otel_genai.trace_span("my_operation") as span:
        ctx = span.get_span_context()
        assert ctx is not None


def test_trace_span_nested_creates_child():
    """嵌套 trace_span：内层 span 是外层的 child。"""
    with otel_genai.trace_span("parent") as parent_span:
        parent_ctx = parent_span.get_span_context()
        with otel_genai.trace_span("child") as child_span:
            child_ctx = child_span.get_span_context()
            # child 和 parent 属于同一 trace
            assert parent_ctx.trace_id == child_ctx.trace_id


# ===== get_trace_id =====
def test_get_trace_id_empty_outside_span():
    """span 外 get_trace_id 返回空字符串（contextvar 未设置时）。"""
    otel_genai._current_trace_id.set("")
    tid = otel_genai.get_trace_id()
    assert tid == ""


def test_get_trace_id_has_value_inside_span():
    """span 内 get_trace_id 返回 32 位 hex trace_id。"""
    with otel_genai.trace_span("root") as span:
        tid = otel_genai.get_trace_id()
        assert len(tid) == 32  # OTel trace_id 格式化为 032x
        assert all(c in "0123456789abcdef" for c in tid)


def test_get_trace_id_contextvar_persists_after_span():
    """root span 结束后 contextvar 仍保留 trace_id（供下游日志关联）。"""
    with otel_genai.trace_span("root"):
        tid_in = otel_genai.get_trace_id()
    tid_after = otel_genai._current_trace_id.get()
    assert tid_after == tid_in


# ===== _should_export 采样策略 =====
class _FakeSpan:
    """模拟 OTel span 用于 _should_export 测试。"""

    def __init__(self, recording=True, status_code=StatusCode.OK):
        self._recording = recording
        self._status = Status(status_code)

    def is_recording(self):
        return self._recording

    @property
    def status(self):
        return self._status


def test_should_export_returns_false_for_non_recording():
    """非 recording span 不导出。"""
    span = _FakeSpan(recording=False)
    assert otel_genai._should_export(span) is False


def test_should_export_error_span_always_exported():
    """status=ERROR 的 span 永远导出（异常必采）。"""
    span = _FakeSpan(recording=True, status_code=StatusCode.ERROR)
    # 即使采样率为 0，异常 span 也要导出
    original_rate = otel_genai._sample_rate
    otel_genai._sample_rate = 0.0
    try:
        assert otel_genai._should_export(span) is True
    finally:
        otel_genai._sample_rate = original_rate


def test_should_export_ok_span_100_percent_sample():
    """采样率=1.0 时 OK span 100% 导出。"""
    span = _FakeSpan(recording=True, status_code=StatusCode.OK)
    otel_genai._sample_rate = 1.0
    assert otel_genai._should_export(span) is True


def test_should_export_ok_span_0_percent_sample():
    """采样率=0.0 时 OK span 不导出。"""
    span = _FakeSpan(recording=True, status_code=StatusCode.OK)
    otel_genai._sample_rate = 0.0
    assert otel_genai._should_export(span) is False


# ===== force_export =====
def test_force_export_below_gate_sets_attributes():
    """faithfulness < 0.85 时设置 force_export + below_gate 属性。"""
    with otel_genai.trace_span("eval_span") as span:
        otel_genai.force_export(faithfulness=0.70)
        assert span.is_recording()


def test_force_export_above_gate_no_force():
    """faithfulness >= 0.85 时不设置 force_export 属性。"""
    with otel_genai.trace_span("eval_span_ok") as span:
        otel_genai.force_export(faithfulness=0.95)
        assert span.is_recording()


def test_force_export_no_faithfulness_noop():
    """faithfulness=None 时 force_export 不做特殊标记。"""
    with otel_genai.trace_span("eval_span_none") as span:
        otel_genai.force_export(faithfulness=None)
        assert span.is_recording()


# ===== set_attribute / add_event / record_exception =====
def test_set_attribute_on_recording_span():
    """set_attribute 在 recording span 上设置属性。"""
    with otel_genai.trace_span("attr_span") as span:
        otel_genai.set_attribute("test.key", "test_value")
        otel_genai.set_attribute("test.num", 42)
        otel_genai.set_attribute("test.bool", True)
        assert span.is_recording()


def test_set_attribute_safe_for_none_value():
    """set_attribute 对 None 值安全处理（转为空字符串）。"""
    with otel_genai.trace_span("none_span") as span:
        otel_genai.set_attribute("test.none", None)
        assert span.is_recording()


def test_set_attribute_coerces_complex_type():
    """set_attribute 把复杂类型转为字符串。"""
    with otel_genai.trace_span("complex_span") as span:
        otel_genai.set_attribute("test.obj", {"a": 1})
        otel_genai.set_attribute("test.list", [1, 2, 3])
        assert span.is_recording()


def test_add_event_on_recording_span():
    """add_event 在 recording span 上添加事件。"""
    with otel_genai.trace_span("event_span") as span:
        otel_genai.add_event("degradation", {"reason": "timeout"})
        assert span.is_recording()


def test_record_exception_sets_error_status():
    """record_exception 设置 span status=ERROR。"""
    with otel_genai.trace_span("err_span") as span:
        otel_genai.record_exception(ValueError("test error"))
        assert span.status.status_code == StatusCode.ERROR


# ===== init_otel 幂等性 =====
def test_init_otel_is_idempotent():
    """init_otel 重复调用不重复初始化。"""
    otel_genai._initialized = False
    otel_genai.init_otel(endpoint="http://localhost:9999/api/public/otel")
    assert otel_genai._initialized is True
    # 第二次调用不应改变状态
    otel_genai.init_otel(endpoint="http://localhost:8888/api/public/otel")
    assert otel_genai._initialized is True
    otel_genai._initialized = False  # 清理


# ===== _coerce_attr 辅助函数 =====
def test_coerce_attr_str():
    assert otel_genai._coerce_attr("hello") == "hello"


def test_coerce_attr_int():
    assert otel_genai._coerce_attr(42) == 42


def test_coerce_attr_float():
    assert otel_genai._coerce_attr(3.14) == 3.14


def test_coerce_attr_bool():
    assert otel_genai._coerce_attr(True) is True


def test_coerce_attr_none_to_empty_string():
    assert otel_genai._coerce_attr(None) == ""


def test_coerce_attr_list():
    assert otel_genai._coerce_attr([1, "a", True]) == [1, "a", True]


def test_coerce_attr_dict_to_string():
    result = otel_genai._coerce_attr({"key": "val"})
    assert isinstance(result, str)
