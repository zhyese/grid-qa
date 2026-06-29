"""本地 bge embedding（sentence-transformers，离线无 API 延迟，解决云限流并发瓶颈）。

CPU 推理放线程池，避免阻塞事件循环。首次加载自动下载模型到 HF 缓存。
"""
import asyncio

from app.config import settings
from app.providers.base import EmbeddingProvider

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(settings.BGE_MODEL)
    return _model


class BgeEmbedding(EmbeddingProvider):
    @property
    def dim(self) -> int:
        return settings.BGE_DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        def _encode():
            vecs = _get_model().encode(texts, normalize_embeddings=True)
            return [v.tolist() for v in vecs]

        return await asyncio.to_thread(_encode)
