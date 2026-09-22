"""DeepSeek LLM（OpenAI 兼容）。"""
from openai import AsyncOpenAI

from app.config import settings
from app.providers.base import LLMProvider


def _extract_content(message) -> str:
    """推理型模型（deepseek-flash/v4-pro）兼容：答案先出 reasoning_content 后出 content。

    LLM_MAX_TOKENS 有限时推理可能耗尽预算、content 全程为空——此时用 reasoning
    兜底返回，避免整答为空触发 LLM_FALLBACK_ON_EMPTY 让位给 fallback 链后位。
    正常响应（content 非空）行为不变。"""
    content = getattr(message, "content", None) or ""
    if content.strip():
        return content
    return getattr(message, "reasoning_content", None) or ""


def _reasoning_kw() -> dict:
    """推理预算控制：DEEPSEEK_REASONING_EFFORT（默认 low，空串=不下发该参数）。

    运维问答要简洁答案：low 让推理只花少量 token，预算留给 content；
    否则 768 max_tokens 可能被推理耗尽、content 全空。"""
    effort = (getattr(settings, "DEEPSEEK_REASONING_EFFORT", "low") or "").strip()
    return {"reasoning_effort": effort} if effort else {}


class DeepSeekLLM(LLMProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.DEEPSEEK_API_KEY, base_url=settings.DEEPSEEK_BASE_URL,
            timeout=settings.LLM_TIMEOUT, max_retries=settings.LLM_MAX_RETRIES,
        )
        self.model = settings.DEEPSEEK_MODEL

    async def chat_with_usage(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw) -> tuple[str, dict | None]:
        from app.core.otel_genai import trace_span, record_exception
        from opentelemetry.trace import SpanKind
        _model = model or self.model
        try:
            with trace_span("llm.generate", kind=SpanKind.CLIENT,
                            attributes={"gen_ai.operation.name": "chat"}):
                r = await self.client.chat.completions.create(
                    model=_model, messages=messages,
                    temperature=temperature, max_tokens=max_tokens,
                    **{**_reasoning_kw(), **kw},
                )
                content = _extract_content(r.choices[0].message)
                usage = None
                if r.usage:
                    usage = {
                        "input": r.usage.prompt_tokens or 0,
                        "output": r.usage.completion_tokens or 0,
                    }
                self._record_llm_span(_model, messages, content, temperature, max_tokens)
                return content, usage
        except Exception as exc:
            record_exception(exc)
            raise

    async def chat(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw) -> str:
        # B4：chat 仍返回 str（向后兼容）；真实 usage 走副通道 chat_with_usage
        content, _ = await self.chat_with_usage(
            messages, temperature=temperature, max_tokens=max_tokens, model=model, **kw)
        return content

    async def stream(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw):
        _model = model or self.model
        r = await self.client.chat.completions.create(
            model=_model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
            stream=True, **{**_reasoning_kw(), **kw},
        )
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        async for chunk in r:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                content_parts.append(piece)
                yield piece
            else:
                rp = getattr(delta, "reasoning_content", None) or ""
                if rp:
                    reasoning_parts.append(rp)
        # 推理型模型 token 耗尽只出了 reasoning：兜底整体吐出（content 正常流不受影响）
        if not content_parts and reasoning_parts:
            yield "".join(reasoning_parts)

    async def chat_with_tools(self, messages, tools, tool_choice="auto", temperature=0.2, max_tokens=2048, model=None, **kw):
        import json as _json
        from app.core.otel_genai import trace_span, record_exception
        from opentelemetry.trace import SpanKind
        _model = model or self.model
        try:
            with trace_span("llm.generate", kind=SpanKind.CLIENT,
                            attributes={"gen_ai.operation.name": "chat_with_tools"}):
                r = await self.client.chat.completions.create(
                    model=_model, messages=messages, tools=tools, tool_choice=tool_choice,
                    temperature=temperature, max_tokens=max_tokens,
                    **{**_reasoning_kw(), **kw},
                )
                msg = r.choices[0].message
                tool_calls = None
                if msg.tool_calls:
                    tool_calls = []
                    for tc in msg.tool_calls:
                        try:
                            args = _json.loads(tc.function.arguments or "{}")
                        except Exception:
                            args = {}
                        tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
                result = {"content": _extract_content(msg), "tool_calls": tool_calls}
                self._record_llm_span(_model, messages, result, temperature, max_tokens)
                return result
        except Exception as exc:
            record_exception(exc)
            raise
