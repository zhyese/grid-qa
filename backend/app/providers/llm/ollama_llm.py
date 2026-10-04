"""本地 Ollama LLM（应急兜底）：云端 fallback 链全灭时兜底作答。

Ollama 原生兼容 OpenAI /v1/chat/completions，复用 openai SDK；api_key 用占位值
（Ollama 不校验）。CPU 推理明显慢于云端 API，用独立的 LLM_LOCAL_TIMEOUT。
不实现 chat_with_tools：本地应急模型不承担 Agent 工具调用职责，沿用基类
NotImplementedError（若被 agent 模式误用会立刻报错，而不是静默返回错误结果）。
"""
from openai import AsyncOpenAI

from app.config import settings
from app.providers import prompt_cache
from app.providers.base import LLMProvider


class OllamaLLM(LLMProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key="ollama", base_url=f"{settings.OLLAMA_BASE_URL}/v1",
            timeout=settings.LLM_LOCAL_TIMEOUT, max_retries=settings.LLM_MAX_RETRIES,
        )
        self.model = settings.OLLAMA_MODEL

    async def chat_with_usage(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw) -> tuple[str, dict | None]:
        _model = model or self.model
        r = await self.client.chat.completions.create(
            model=_model, messages=messages,
            temperature=temperature, max_tokens=max_tokens, **kw,
        )
        content = r.choices[0].message.content
        usage = prompt_cache.usage_dict(r.usage)
        return content, usage

    async def chat(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw) -> str:
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
