"""DeepSeek LLM（OpenAI 兼容）。"""
from openai import AsyncOpenAI

from app.config import settings
from app.providers.base import LLMProvider


class DeepSeekLLM(LLMProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.DEEPSEEK_API_KEY, base_url=settings.DEEPSEEK_BASE_URL
        )
        self.model = settings.DEEPSEEK_MODEL

    async def chat(self, messages, temperature=0.2, max_tokens=2048, **kw) -> str:
        from app.core.otel_genai import trace_span, record_exception
        from opentelemetry.trace import SpanKind
        try:
            with trace_span("llm.generate", kind=SpanKind.CLIENT,
                            attributes={"gen_ai.operation.name": "chat"}):
                r = await self.client.chat.completions.create(
                    model=self.model, messages=messages,
                    temperature=temperature, max_tokens=max_tokens, **kw,
                )
                content = r.choices[0].message.content
                self._record_llm_span(self.model, messages, content, temperature, max_tokens)
                return content
        except Exception as exc:
            record_exception(exc)
            raise

    async def stream(self, messages, temperature=0.2, max_tokens=2048, **kw):
        r = await self.client.chat.completions.create(
            model=self.model, messages=messages,
            temperature=temperature, max_tokens=max_tokens, stream=True, **kw,
        )
        async for chunk in r:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def chat_with_tools(self, messages, tools, tool_choice="auto", temperature=0.2, max_tokens=2048, **kw):
        import json as _json
        from app.core.otel_genai import trace_span, record_exception
        from opentelemetry.trace import SpanKind
        try:
            with trace_span("llm.generate", kind=SpanKind.CLIENT,
                            attributes={"gen_ai.operation.name": "chat_with_tools"}):
                r = await self.client.chat.completions.create(
                    model=self.model, messages=messages, tools=tools, tool_choice=tool_choice,
                    temperature=temperature, max_tokens=max_tokens, **kw,
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
                self._record_llm_span(self.model, messages, result, temperature, max_tokens)
                return result
        except Exception as exc:
            record_exception(exc)
            raise
