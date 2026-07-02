"""火山方舟豆包 LLM（OpenAI 兼容）。model 字段传推理接入点 endpoint_id。"""
from openai import AsyncOpenAI

from app.config import settings
from app.providers.base import LLMProvider


class DoubaoLLM(LLMProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.ARK_API_KEY, base_url=settings.ARK_BASE_URL
        )
        self.model = settings.DOUBAO_LLM_ENDPOINT_ID  # ep-xxxx，非模型名

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

    async def chat_with_tools(self, messages, tools, tool_choice="auto", temperature=0.2, max_tokens=2048, **kw):
        import json as _json
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
        return {"content": msg.content, "tool_calls": tool_calls}
