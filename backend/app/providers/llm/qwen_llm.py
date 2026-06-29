"""阿里百炼 Qwen LLM（OpenAI 兼容）。"""
from openai import AsyncOpenAI

from app.config import settings
from app.providers.base import LLMProvider


class QwenLLM(LLMProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.DASHSCOPE_API_KEY, base_url=settings.DASHSCOPE_BASE_URL
        )
        self.model = settings.QWEN_LLM_MODEL

    async def chat(self, messages, temperature=0.2, max_tokens=2048, **kw) -> str:
        r = await self.client.chat.completions.create(
            model=self.model, messages=messages,
            temperature=temperature, max_tokens=max_tokens, **kw,
        )
        return r.choices[0].message.content
