"""阿里百炼 Qwen LLM（OpenAI 兼容）。

F7 Prompt Caching：显式缓存标记（cache_control:{type:ephemeral}）加在稳定前缀上，
网关不支持时自动去标记重试一次（见 providers/prompt_cache.py）。
"""
from openai import AsyncOpenAI

from app.config import settings
from app.providers import prompt_cache
from app.providers.base import LLMProvider


class QwenLLM(LLMProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.DASHSCOPE_API_KEY, base_url=settings.DASHSCOPE_BASE_URL,
            timeout=settings.LLM_TIMEOUT, max_retries=settings.LLM_MAX_RETRIES,
        )
        self.model = settings.QWEN_LLM_MODEL

    async def chat_with_usage(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw) -> tuple[str, dict | None]:
        from app.core.otel_genai import trace_span, record_exception
        from opentelemetry.trace import SpanKind
        _model = model or self.model
        try:
            with trace_span("llm.generate", kind=SpanKind.CLIENT,
                            attributes={"gen_ai.operation.name": "chat"}):
                r = await prompt_cache.call_with_cache_fallback(
                    lambda msgs: self.client.chat.completions.create(
                        model=_model, messages=msgs,
                        temperature=temperature, max_tokens=max_tokens, **kw,
                    ),
                    messages, "qwen",
                )
                content = r.choices[0].message.content
                usage = prompt_cache.usage_dict(r.usage)
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
        marked = prompt_cache.apply_explicit_cache(messages, "qwen")
        try:
            r = await self.client.chat.completions.create(
                model=_model, messages=marked,
                temperature=temperature, max_tokens=max_tokens, stream=True, **kw,
            )
        except Exception as exc:
            if marked is not messages and prompt_cache.is_cache_param_error(exc):
                from app.core.obs import degraded
                degraded("prompt_cache_param_unsupported", exc, "qwen")
                r = await self.client.chat.completions.create(
                    model=_model, messages=messages,
                    temperature=temperature, max_tokens=max_tokens, stream=True, **kw,
                )
            else:
                raise
        async for chunk in r:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def chat_with_tools(self, messages, tools, tool_choice="auto", temperature=0.2, max_tokens=2048, model=None, **kw):
        import json as _json
        from app.core.otel_genai import trace_span, record_exception
        from opentelemetry.trace import SpanKind
        _model = model or self.model
        try:
            with trace_span("llm.generate", kind=SpanKind.CLIENT,
                            attributes={"gen_ai.operation.name": "chat_with_tools"}):
                r = await prompt_cache.call_with_cache_fallback(
                    lambda msgs: self.client.chat.completions.create(
                        model=_model, messages=msgs, tools=tools, tool_choice=tool_choice,
                        temperature=temperature, max_tokens=max_tokens, **kw,
                    ),
                    messages, "qwen",
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
                result = {"content": msg.content, "tool_calls": tool_calls}
                self._record_llm_span(_model, messages, result, temperature, max_tokens)
                return result
        except Exception as exc:
            record_exception(exc)
            raise
