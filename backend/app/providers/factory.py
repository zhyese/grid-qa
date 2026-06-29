"""配置驱动的 Provider 工厂。EMB_PROVIDER / LLM_PROVIDER 切换底层实现。

nacos 预留点：后续在此读取 NacosConfigSource 替代 settings 即可，业务零改动。
"""
from app.config import settings
from app.providers.base import EmbeddingProvider, LLMProvider


def get_embedding_provider(provider: str | None = None) -> EmbeddingProvider:
    p = provider or settings.EMB_PROVIDER
    if p == "qwen":
        from app.providers.embedding.qwen_embedding import QwenEmbedding
        return QwenEmbedding()
    if p == "doubao":
        from app.providers.embedding.doubao_embedding import DoubaoEmbedding
        return DoubaoEmbedding()
    if p == "bge":
        from app.providers.embedding.bge_embedding import BgeEmbedding
        return BgeEmbedding()
    raise ValueError(f"未知 EMB_PROVIDER: {p}（支持: qwen | doubao | bge）")


def get_llm_provider(provider: str | None = None) -> LLMProvider:
    p = provider or settings.LLM_PROVIDER
    if p == "deepseek":
        from app.providers.llm.deepseek_llm import DeepSeekLLM
        return DeepSeekLLM()
    if p == "qwen":
        from app.providers.llm.qwen_llm import QwenLLM
        return QwenLLM()
    if p == "doubao":
        from app.providers.llm.doubao_llm import DoubaoLLM
        return DoubaoLLM()
    raise ValueError(f"未知 LLM_PROVIDER: {p}（支持: deepseek | qwen | doubao）")
