"""阿里百炼 DashScope text-embedding-v3（OpenAI 兼容）。默认 1024 维。"""
from openai import AsyncOpenAI

from app.config import settings
from app.providers.base import EmbeddingProvider

_BATCH = 10  # 百炼单次 embedding 上限 10 条


class QwenEmbedding(EmbeddingProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.DASHSCOPE_API_KEY, base_url=settings.DASHSCOPE_BASE_URL
        )
        self.model = settings.QWEN_EMB_MODEL

    @property
    def dim(self) -> int:
        return settings.EMBEDDING_DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for i in range(0, len(texts), _BATCH):
            batch = texts[i : i + _BATCH]
            r = await self.client.embeddings.create(model=self.model, input=batch)
            result.extend([d.embedding for d in r.data])
        return result
