"""配置驱动的 Provider 工厂。EMB_PROVIDER / LLM_PROVIDER 切换底层实现。

nacos 预留点：后续在此读取 NacosConfigSource 替代 settings 即可，业务零改动。
"""
from app.config import settings
from app.providers.base import EmbeddingProvider, LLMProvider


def get_embedding_provider() -> EmbeddingProvider:
    p = settings.EMB_PROVIDER
    if p == "qwen":
        from app.providers.embedding.qwen_embedding import QwenEmbedding
        return QwenEmbedding()
    if p == "doubao":
        from app.providers.embedding.doubao_embedding import DoubaoEmbedding
        return DoubaoEmbedding()
    raise ValueError(f"未知 EMB_PROVIDER: {p}（支持: qwen | doubao）")


def get_llm_provider() -> LLMProvider:  # S7 实现
    from app.providers.base import LLMProvider  # noqa
    raise NotImplementedError("LLM provider 在 S7 实现")
