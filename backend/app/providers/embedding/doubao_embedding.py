"""火山方舟 Ark doubao-embedding（OpenAI 兼容）。默认 2048 维，传 dimensions 降维到 1024 对齐。"""
from openai import AsyncOpenAI

from app.config import settings
from app.providers.base import EmbeddingProvider

_BATCH = 10


class DoubaoEmbedding(EmbeddingProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.ARK_API_KEY, base_url=settings.ARK_BASE_URL
        )
        self.model = settings.DOUBAO_EMB_MODEL

    @property
    def dim(self) -> int:
        return settings.EMBEDDING_DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for i in range(0, len(texts), _BATCH):
            batch = texts[i : i + _BATCH]
            r = await self.client.embeddings.create(
                model=self.model, input=batch,
                dimensions=settings.EMBEDDING_DIM,  # 降维到 1024 与百炼对齐
                encoding_format="float",
            )
            result.extend([d.embedding for d in r.data])
        return result
