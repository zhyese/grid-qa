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


def _get_raw_provider(p: str) -> LLMProvider:
    """构造单个原始 provider 实例（不包 fallback）。check_llm_health/探活用，避免掩盖故障。"""
    if p == "deepseek":
        from app.providers.llm.deepseek_llm import DeepSeekLLM
        return DeepSeekLLM()
    if p == "qwen":
        from app.providers.llm.qwen_llm import QwenLLM
        return QwenLLM()
    if p == "doubao":
        from app.providers.llm.doubao_llm import DoubaoLLM
        return DoubaoLLM()
    if p == "ollama":
        from app.providers.llm.ollama_llm import OllamaLLM
        return OllamaLLM()
    raise ValueError(f"未知 LLM_PROVIDER: {p}（支持: deepseek | qwen | doubao | ollama）")


def get_llm_provider(provider: str | None = None, tier: str = "plus") -> LLMProvider:
    """返回带 fallback + 健康度熔断的 provider wrapper（L0/L1 自动生效，对调用方透明）。

    provider 为空/占位(default/auto) → 走 fallback 链头；用户显式选优先（不破坏前端手选）。
    resolve_chain 给有序候选名，按 is_healthy 过滤；全不健康时返回全链（靠 L0 兜底，不阻断）。
    tier：L2 分档（turbo/plus），影响 provider 的 model override。
    """
    from app.providers.llm_router import resolve_chain, is_healthy, FallbackLLMProvider
    names = resolve_chain(provider)
    if not names:
        names = [settings.LLM_PROVIDER]
    healthy = [n for n in names if is_healthy(n)] or names
    providers = [_get_raw_provider(n) for n in healthy]
    return FallbackLLMProvider(providers, healthy, tier=tier)


async def check_llm_health(provider: str | None = None) -> dict:
    """主动探测当前 LLM provider 是否可用（轻量 ping，按需调用消耗少量 token）。

    用于抓 key 失效 / 账户欠费(Arrearage) / 配额耗尽 / 网络问题等配置无法发现的运行态故障。
    """
    p = provider or settings.LLM_PROVIDER
    try:
        # 用原始 provider（不包 fallback），否则 fallback 会掩盖单 provider 故障 → 熔断永不触发
        await _get_raw_provider(p).chat(
            [{"role": "user", "content": "ping"}], max_tokens=5, temperature=0
        )
        return {"provider": p, "status": "ok"}
    except Exception as e:
        return {"provider": p, "status": "error", "error": f"{type(e).__name__}: {e}"[:200]}


async def check_embedding_health(provider: str | None = None) -> dict:
    """主动探测当前 Embedding provider 是否可用（嵌入一条短文本）。

    H1：额外回报实际模型名/维度/向量空间签名，以及**向量库漂移状态**——
    换模型后未重建 Milvus 是"检索静默变差"的头号原因，这里让它可见。
    """
    p = provider or settings.EMB_PROVIDER
    out: dict = {"provider": p}
    # 1) 主动探测：真打一次短文本，拿实际维度
    try:
        prov = get_embedding_provider(p)
        out["model"] = getattr(prov, "model", settings.BGE_MODEL if p == "bge" else "")
        out["dim"] = prov.dim
        out["signature"] = prov.model_signature
        vecs = await prov.embed(["健康检查"])
        actual_dim = len(vecs[0]) if vecs and vecs[0] else None
        out.update({"status": "ok", "actualDim": actual_dim})
        if actual_dim is not None and actual_dim != prov.dim:
            out["status"] = "error"
            out["error"] = f"实际返回维度 {actual_dim} 与配置 EMBEDDING_DIM={prov.dim} 不一致"
    except Exception as e:
        out.update({"status": "error", "error": f"{type(e).__name__}: {e}"[:200]})
    # 2) 向量空间守卫信息：**与探测结果无关**——provider 欠费/宕机时更要能看见"待重建"，
    #    否则运维只看到"embedding 挂了"，不知道重建这件事还欠着。
    try:
        from app.services.embedding_service import ensure_model_consistency, rebuild_pending
        guard = await ensure_model_consistency(p)
        out["vectorSpaceStatus"] = guard.get("status")
        out["rebuildPending"] = bool(guard.get("rebuildPending"))
        # 待重建状态以 Redis 为准（进程内缓存可能滞后），并带置位时间便于判断拖了多久
        pend = await rebuild_pending(p)
        if pend.get("pending"):
            out["rebuildPending"] = True
            out["rebuildPendingSince"] = pend.get("since", "")
            out["rebuildPendingReason"] = pend.get("reason", "")
        if guard.get("status") == "drift" or out["rebuildPending"]:
            out["driftMessage"] = guard.get("message", "")
    except Exception:
        pass
    return out
