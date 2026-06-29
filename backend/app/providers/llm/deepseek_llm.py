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
        r = await self.client.chat.completions.create(
            model=self.model, messages=messages,
            temperature=temperature, max_tokens=max_tokens, **kw,
        )
        return r.choices[0].message.content

    async def stream(self, messages, temperature=0.2, max_tokens=2048, **kw):
        r = await self.client.chat.completions.create(
            model=self.model, messages=messages,
            temperature=temperature, max_tokens=max_tokens, stream=True, **kw,
        )
        async for chunk in r:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
