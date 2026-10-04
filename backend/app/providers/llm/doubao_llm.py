"""火山方舟豆包 LLM（OpenAI 兼容）。model 字段传推理接入点 endpoint_id。

F7 Prompt Caching：方舟走网关隐式前缀缓存，本项目不传显式缓存参数；
仅把网关返回的 usage.prompt_tokens_details.cached_tokens 归一化进 usage dict 供成本核算。
"""
from openai import AsyncOpenAI

from app.config import settings
from app.providers import prompt_cache
from app.providers.base import LLMProvider


class DoubaoLLM(LLMProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.ARK_API_KEY, base_url=settings.ARK_BASE_URL,
            timeout=settings.LLM_TIMEOUT, max_retries=settings.LLM_MAX_RETRIES,
        )
        self.model = settings.DOUBAO_LLM_ENDPOINT_ID  # ep-xxxx，非模型名

    async def chat_with_usage(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw) -> tuple[str, dict | None]:
        from app.core.otel_genai import trace_span, record_exception
        from opentelemetry.trace import SpanKind
        _model = model or self.model
        try:
            with trace_span("llm.generate", kind=SpanKind.CLIENT,
                            attributes={"gen_ai.operation.name": "chat"}):
                r = await self.client.chat.completions.create(
                    model=_model, messages=messages,
                    temperature=temperature, max_tokens=max_tokens, **kw,
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
        r = await self.client.chat.completions.create(
            model=_model, messages=messages,
            temperature=temperature, max_tokens=max_tokens, stream=True, **kw,
        )
        async for chunk in r:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def chat_with_tools(self, messages, tools, tool_choice="auto", temperature=0.2, max_tokens=2048, model=None, **kw):
        import json as _json
        _model = model or self.model
        r = await self.client.chat.completions.create(
            model=_model, messages=messages, tools=tools, tool_choice=tool_choice,
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
        return {"content": msg.content, "tool_calls": tool_calls}
