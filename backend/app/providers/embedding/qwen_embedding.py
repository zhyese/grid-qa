"""阿里百炼 DashScope text-embedding-v3（OpenAI 兼容）。默认 1024 维。"""
import asyncio

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
        if not texts:
            return []
        batches = [texts[i:i + _BATCH] for i in range(0, len(texts), _BATCH)]
        sem = asyncio.Semaphore(max(1, getattr(settings, "EMB_BATCH_CONCURRENCY", 3)))

        async def _one(batch: list[str]) -> list[list[float]]:
            async with sem:  # 并发限流防 429
                r = await self.client.embeddings.create(model=self.model, input=batch)
                return [d.embedding for d in r.data]

        parts = await asyncio.gather(*[_one(b) for b in batches])  # 批次并发，结果按序拼接
        return [vec for part in parts for vec in part]
