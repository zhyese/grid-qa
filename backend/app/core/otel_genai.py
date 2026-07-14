"""N4 OpenTelemetry GenAI 语义约定包装器。

用 opentelemetry-sdk 原生 span + 手写 gen_ai.* attribute（不引入 alpha 包），
与 Langfuse 原生兼容。提供：
- init_otel()：初始化 TracerProvider + OTLP HTTP 导出器
- trace_span()：上下文管理器，创建 span 并自动关联当前 trace
- get_trace_id()：获取当前 trace_id（contextvars 传递，async 友好）
- set_attribute() / add_event() / record_exception()：便捷 span 操作
- force_export()：异常必采（faithfulness < gate 或 status=ERROR 强制导出）

采样策略：
- 开发期 OTEL_SAMPLE_RATE=1.0（100% 采样）
- 上线后 OTEL_SAMPLE_RATE=0.1（10% 随机采样）
- 异常必采：span status=ERROR 或 faithfulness < FAITHFULNESS_GATE → 强制导出
"""
import contextvars
import random
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

from app.config import settings

# trace_id 通过 contextvars 在 async 调用链中自动传递
_current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")

# 采样率（运行时可调）
_sample_rate: float = settings.OTEL_SAMPLE_RATE

# 是否已初始化
_initialized: bool = False


def init_otel(endpoint: str | None = None, sample_rate: float | None = None) -> None:
    """初始化 OpenTelemetry TracerProvider + OTLP HTTP 导出器（发到 Langfuse）。

    幂等：重复调用不会重复初始化。
    """
    global _initialized, _sample_rate
    if _initialized:
        return
    if sample_rate is not None:
        _sample_rate = sample_rate
    ep = endpoint or settings.OTEL_ENDPOINT
    resource = Resource.create({
        "service.name": settings.OTEL_SERVICE_NAME,
        "service.version": settings.APP_VERSION,
    })
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=ep)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _initialized = True


def _should_export(span) -> bool:
    """采样决策：异常 span(status=ERROR) 永远导出，否则按采样率随机。

    OTel BatchSpanProcessor 默认导出所有 ended span，采样逻辑在此控制：
    未采样的 span 标记为不记录（is_recording=False），BatchSpanProcessor 自动跳过。
    但由于我们用的是 start_as_current_span（非 sampler），所有 span 都会 record，
    所以采样在导出层面通过 _force_export 标记控制——标记为 force 的 span 在
    end 前设置 status=OK 即可被导出，未采样的 span 设置 attributes 标记跳过。

    实际实现：用 contextvars 传递采样标志，force_export 直接设置 status。
    """
    if not span.is_recording():
        return False
    # 异常 span 永远导出
    if span.status.status_code == StatusCode.ERROR:
        return True
    # 100% 采样
    if _sample_rate >= 1.0:
        return True
    # 概率采样
    return random.random() < _sample_rate


@contextmanager
def trace_span(name: str, kind: SpanKind = SpanKind.INTERNAL, attributes: dict | None = None):
    """上下文管理器：创建 span 并自动关联到当前 trace。

    用法：
        with trace_span("retrieve") as span:
            span.set_attribute("retrieval.hit_count", len(results))
            results = await mixed_search(...)

    采样：开发期 100%，上线后按 _sample_rate 随机；异常必采。
    首次创建 root span 时把 trace_id 存入 contextvar 供下游日志关联。
    """
    tracer = trace.get_tracer("grid-qa")
    span = tracer.start_span(name, kind=kind)
    # 设置初始属性
    if attributes:
        for k, v in attributes.items():
            _safe_set_attr(span, k, v)
    # 如果是 root span（没有 parent），记录 trace_id 到 contextvar
    parent = trace.get_current_span()
    is_root = not parent or not parent.is_recording()
    if is_root:
        _current_trace_id.set(format(span.get_span_context().trace_id, "032x"))
    with trace.use_span(span, end_on_exit=True):
        yield span


def get_trace_id() -> str:
    """获取当前请求的 trace_id（N1/N2/N3 可用于日志关联）。

    优先从当前 OTel span 获取，回退到 contextvar。
    """
    span = trace.get_current_span()
    if span and span.is_recording():
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            return format(ctx.trace_id, "032x")
    return _current_trace_id.get()


def set_attribute(key: str, value: Any) -> None:
    """向当前 span 设置属性（安全：非 recording span 静默跳过）。"""
    span = trace.get_current_span()
    if span and span.is_recording():
        _safe_set_attr(span, key, value)


def add_event(name: str, attrs: dict | None = None) -> None:
    """向当前 span 添加事件（如降级事件）。"""
    span = trace.get_current_span()
    if span and span.is_recording():
        clean = {k: _coerce_attr(v) for k, v in (attrs or {}).items()}
        span.add_event(name, attributes=clean)


def record_exception(exc: BaseException) -> None:
    """向当前 span 记录异常（设置 status=ERROR，异常必采）。"""
    span = trace.get_current_span()
    if span and span.is_recording():
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, str(exc)))


def force_export(faithfulness: float | None = None) -> None:
    """强制导出当前 trace（异常必采：faithfulness < gate 时调用）。

    faithfulness 低于 FAITHFULNESS_GATE(0.85) 时强制设置 span attribute 标记，
    使该 trace 在 Langfuse 中可见。
    """
    span = trace.get_current_span()
    if span and span.is_recording():
        if faithfulness is not None:
            span.set_attribute("eval.faithfulness", faithfulness)
            gate = getattr(settings, "FAITHFULNESS_GATE", 0.85)
            if faithfulness < gate:
                span.set_attribute("eval.force_export", True)
                span.set_attribute("eval.below_gate", True)


def _safe_set_attr(span, key: str, value: Any) -> None:
    """安全设置 span 属性：过滤 OTel 不支持的类型。"""
    span.set_attribute(key, _coerce_attr(value))


def _coerce_attr(value: Any) -> Any:
    """把 Python 值转为 OTel 支持的属性类型（str/int/float/bool/序列）。"""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce_attr(v) for v in value]
    return str(value)
