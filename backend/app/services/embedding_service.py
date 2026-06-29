"""向量生成服务：批量/单条，委托给配置的 EmbeddingProvider。"""
from app.providers.factory import get_embedding_provider


async def embed_texts(texts: list[str]) -> list[list[float]]:
    return await get_embedding_provider().embed(texts)


async def embed_query(text: str) -> list[float]:
    return (await get_embedding_provider().embed([text]))[0]
