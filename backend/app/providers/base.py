"""模型 Provider 抽象层。三家云服务均兼容 OpenAI 协议，统一用 openai SDK 对接。"""
from abc import ABC, abstractmethod
from typing import AsyncIterator


class LLMProvider(ABC):
    """对话/生成 LLM。"""

    @abstractmethod
    async def chat(self, messages: list[dict], temperature: float = 0.2,
                   max_tokens: int = 2048, **kwargs) -> str: ...

    async def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        raise NotImplementedError

    async def chat_with_tools(self, messages: list[dict], tools: list[dict],
                              tool_choice: str = "auto", temperature: float = 0.2,
                              max_tokens: int = 2048, **kwargs) -> dict:
        """function-calling：返回 {"content": str|None, "tool_calls": [{id,name,arguments:dict}]|None}。
        子类用 openai SDK 透传 tools=。"""
        raise NotImplementedError


class EmbeddingProvider(ABC):
    """向量化。dim 固定 1024（两家 embedding 统一对齐）。"""

    @property
    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
