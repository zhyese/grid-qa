"""模型 Provider 抽象层。三家云服务均兼容 OpenAI 协议，统一用 openai SDK 对接。"""
from abc import ABC, abstractmethod
from typing import AsyncIterator


class LLMProvider(ABC):
    """对话/生成 LLM。

    N4：chat/chat_with_tools 内部包 otel_genai LLM span（gen_ai.* 语义约定）。
    子类在 chat 返回后调用 _record_llm_span 记录 token/模型属性（可选）。
    """

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

    @staticmethod
    def _record_llm_span(model: str, messages: list[dict], result: str | dict,
                         temperature: float, max_tokens: int) -> None:
        """N4：记录 LLM span 属性（gen_ai.* 语义约定，Langfuse 兼容）。

        在 chat/chat_with_tools 返回后调用。安全：OTel 未初始化时静默跳过。
        """
        try:
            from app.core.otel_genai import set_attribute
            set_attribute("gen_ai.system", "openai")
            set_attribute("gen_ai.request.model", model)
            set_attribute("gen_ai.request.temperature", temperature)
            set_attribute("gen_ai.request.max_tokens", max_tokens)
            set_attribute("gen_ai.request.message_count", len(messages))
            if isinstance(result, str):
                set_attribute("gen_ai.response.content_length", len(result))
            elif isinstance(result, dict):
                set_attribute("gen_ai.response.has_tool_calls",
                              bool(result.get("tool_calls")))
        except Exception:
            pass


class EmbeddingProvider(ABC):
    """向量化。dim 固定 1024（两家 embedding 统一对齐）。"""

    @property
    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
