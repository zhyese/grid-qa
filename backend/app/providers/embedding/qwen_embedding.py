"""阿里百炼 DashScope 文本向量（OpenAI 兼容）。

H1 检索底座代际升级：
- 默认模型 `text-embedding-v4`（Qwen3-Embedding 系，MTEB 多语言榜前列），
  支持 64~2048 维自定义，本项目固定 1024 维与既有 collection 对齐。
- `text-embedding-v3` 亦可（切回只需改 `QWEN_EMB_MODEL`，但要**重建 Milvus**）。
- 单次请求条数上限：v4 为 20、v3 为 10 → 取保守值 `EMB_BATCH_SIZE`（默认 10），
  调大前请确认网关实际限额，防 400。
- `dimensions` 参数：新模型支持自定义维度，显式下发以钉死 1024；
  网关/旧模型不认该参数时自动去掉重试一次（不因参数把向量化打挂）。
"""
import asyncio

from openai import AsyncOpenAI

from app.config import settings
from app.core.obs import degraded
from app.providers.base import EmbeddingProvider


class QwenEmbedding(EmbeddingProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.DASHSCOPE_API_KEY, base_url=settings.DASHSCOPE_BASE_URL
        )
        self.model = settings.QWEN_EMB_MODEL

    @property
    def dim(self) -> int:
        return settings.EMBEDDING_DIM

    @property
    def model_signature(self) -> str:
        return f"qwen:{self.model}:{self.dim}"

    async def _create(self, batch: list[str], with_dim: bool):
        kw = {"dimensions": settings.EMBEDDING_DIM} if with_dim else {}
        return await self.client.embeddings.create(model=self.model, input=batch, **kw)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        batch_size = max(1, int(getattr(settings, "EMB_BATCH_SIZE", 10)))
        batches = [texts[i:i + batch_size] for i in range(0, len(texts), batch_size)]
        sem = asyncio.Semaphore(max(1, getattr(settings, "EMB_BATCH_CONCURRENCY", 3)))

        async def _one(batch: list[str]) -> list[list[float]]:
            async with sem:  # 并发限流防 429
                try:
                    r = await self._create(batch, with_dim=True)
                except Exception as exc:
                    # 网关/模型不认 dimensions（旧模型、代理网关裁剪参数）→ 去掉重试一次
                    if "dimensions" not in str(exc).lower():
                        raise
                    degraded("embed_dimensions_unsupported", exc, self.model)
                    r = await self._create(batch, with_dim=False)
                return [d.embedding for d in r.data]

        parts = await asyncio.gather(*[_one(b) for b in batches])  # 批次并发，结果按序拼接
        return [vec for part in parts for vec in part]
