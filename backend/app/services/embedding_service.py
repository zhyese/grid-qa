"""向量生成服务：批量/单条，委托给配置的 EmbeddingProvider。query 向量走 Redis 缓存。

H1：额外承担「向量空间一致性守卫」——换 embedding 模型/维度后，Milvus 里的旧向量与新
query 向量不可比，检索会**静默全错**（不报错、只是召回变差）。守卫把首次使用的模型签名
落 Redis，之后每次启动/首次调用比对，漂移即 degraded + 健康检查可见，强制走重建流程。
"""
import json
from datetime import datetime

from app.clients import redis_client
from app.config import settings
from app.core import metrics
from app.core.obs import degraded
from app.providers.factory import get_embedding_provider

# ---------- H1 向量空间一致性守卫 ----------
_SIG_KEY = "embed:model_sig:{provider}"
# 「待重建」持久标记：签名对不上、或首次建基线时库内已有存量（无法确认存量用的是哪个模型）
# 都会置位；只有全量重建成功（mark_vectors_rebuilt）才清除。
# 为什么需要它：仅靠签名比对有漏洞——基线一旦写进去就报 ok，而"存量向量其实是旧模型"
# 这件事签名比对看不出来。标记持久化后，每次启动与健康检查都会持续提醒，不会被遗忘。
_PENDING_KEY = "embed:rebuild_pending:{provider}"
# 进程内缓存：避免每次 embed 都打 Redis（首次调用查一次即可）
_sig_state: dict[str, dict] = {}


def _guard_enabled() -> bool:
    return bool(getattr(settings, "EMBED_MODEL_GUARD_ENABLE", True))


def _signature_of(provider: str) -> str:
    return get_embedding_provider(provider).model_signature


def _collection_has_vectors(provider: str) -> bool:
    """向量库是否已有存量（用于首次建立基线时的风险提示）。失败返回 False（不打扰）。"""
    try:
        from app.clients import milvus_client
        if provider == "bge":
            name = settings.MILVUS_COLLECTION_BGE
        else:
            name = settings.MILVUS_COLLECTION
        return int(milvus_client.num_entities(name) or 0) > 0
    except Exception:
        return False


def _pending_hint(sig: str) -> str:
    return (
        f"⚠️ 向量库待重建（当前模型签名 {sig}）：存量向量可能由旧模型生成，"
        "新旧向量不在同一空间，云检索会返回错误结果且**不报错**。"
        "请执行 `docker exec grid-backend python /app/scripts/rebuild_embeddings.py --yes` "
        "（本机：`python scripts/rebuild_embeddings.py --yes`）完成重建。"
    )


async def rebuild_pending(provider: str | None = None) -> dict:
    """只读查询「待重建」状态（健康检查用，不产生副作用）。"""
    p = provider or settings.EMB_PROVIDER
    out = {"provider": p, "pending": False, "since": "", "signature": ""}
    try:
        raw = await redis_client.get_redis().get(_PENDING_KEY.format(provider=p))
        if raw:
            raw = raw.decode() if isinstance(raw, bytes) else raw
            data = json.loads(raw)
            out.update({"pending": True, "since": data.get("since", ""),
                        "signature": data.get("signature", ""),
                        "reason": data.get("reason", "")})
    except Exception as e:
        degraded("embed_rebuild_pending_read", e, p)
    return out


async def _set_pending(p: str, sig: str, reason: str) -> None:
    """置位待重建标记（幂等：已存在则保留最早的 since，便于判断拖了多久）。"""
    try:
        r = redis_client.get_redis()
        key = _PENDING_KEY.format(provider=p)
        if await r.get(key):
            return
        await r.set(key, json.dumps({
            "since": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "signature": sig, "reason": reason,
        }, ensure_ascii=False))
    except Exception as e:
        degraded("embed_rebuild_pending_set", e, p)


async def ensure_model_consistency(provider: str | None = None) -> dict:
    """比对当前 embedding 模型签名与向量库构建时记录（H1）。

    返回 {provider, signature, status, previous, message, rebuildPending}，status ∈
    ok（一致）/ initialized（首次记录）/ drift（已漂移，需重建）/ disabled / error。
    绝不抛异常——守卫失败不能影响向量化主链路。
    """
    p = provider or settings.EMB_PROVIDER
    if not _guard_enabled():
        return {"provider": p, "status": "disabled", "rebuildPending": False,
                "message": "守卫已关闭（EMBED_MODEL_GUARD_ENABLE=false）"}
    cached = _sig_state.get(p)
    if cached:
        return cached
    try:
        sig = _signature_of(p)
    except Exception as e:
        degraded("embed_sig_resolve", e, p)
        return {"provider": p, "status": "error", "rebuildPending": False,
                "message": f"无法解析模型签名：{e}"}
    result = {"provider": p, "signature": sig, "previous": "", "status": "ok",
              "rebuildPending": False, "message": ""}
    try:
        r = redis_client.get_redis()
        key = _SIG_KEY.format(provider=p)
        prev = await r.get(key)
        prev = prev.decode() if isinstance(prev, bytes) else prev
        if not prev:
            await r.set(key, sig)
            result["status"] = "initialized"
            result["message"] = f"已记录当前向量空间签名 {sig}"
            # 首次建立基线（含本次升级）时，若库内已有存量向量 → 无法确认存量模型，
            # 置「待重建」标记并持续提醒（不能只在启动时喊一次）。
            if _collection_has_vectors(p):
                await _set_pending(p, sig, "首次建立签名基线时库内已有存量向量")
                result["rebuildPending"] = True
                result["message"] += "；" + _pending_hint(sig)
                degraded("embed_model_baseline_new", RuntimeError(_pending_hint(sig)), p)
        elif prev != sig:
            result["status"] = "drift"
            result["previous"] = prev
            await _set_pending(p, sig, f"签名漂移：{prev} → {sig}")
            result["rebuildPending"] = True
            result["message"] = (
                f"向量空间漂移：向量库构建时为 {prev}，当前为 {sig}。"
                "旧向量与新 query 不可比，检索结果不可信——请执行 "
                "`python scripts/rebuild_embeddings.py` 重建后重试。"
            )
            degraded("embed_model_drift", RuntimeError(result["message"]), p)
        else:
            pend = await rebuild_pending(p)
            result["rebuildPending"] = bool(pend.get("pending"))
            result["message"] = ("向量空间签名一致" if not result["rebuildPending"]
                                 else _pending_hint(sig))
    except Exception as e:
        # Redis 不可用 → 降级为不校验（不阻断向量化）
        degraded("embed_sig_guard", e, p)
        return {"provider": p, "signature": sig, "previous": "", "status": "error",
                "rebuildPending": False, "message": f"签名校验降级（Redis 异常）：{e}"}
    _sig_state[p] = result
    return result


def reset_model_signature_cache() -> None:
    """清进程内缓存（重建向量后调用，让守卫重新比对）。"""
    _sig_state.clear()


async def mark_vectors_rebuilt(provider: str | None = None) -> None:
    """向量库**全量**重建成功后调用：重置签名 + 清除「待重建」标记。

    注意：局部重建（--limit / --doc-ids）或存在失败文档时**不得**调用本函数，
    否则混合向量空间会被伪装成正常（见 scripts/rebuild_embeddings.py 的分支处理）。
    """
    p = provider or settings.EMB_PROVIDER
    reset_model_signature_cache()
    try:
        sig = _signature_of(p)
        r = redis_client.get_redis()
        await r.set(_SIG_KEY.format(provider=p), sig)
        await r.delete(_PENDING_KEY.format(provider=p))
    except Exception as e:
        degraded("embed_sig_reset", e, p)


async def embed_texts(
    texts: list[str],
    chunk_ids: list[str] | None = None,
) -> list[list[float]]:
    """批量向量化，委托给配置的 EmbeddingProvider。

    A1/A4（Batch 2）：可选 chunk_ids 实现 chunk 向量复用。
    - 开关 EMBED_CHUNK_CACHE_ENABLE 默认关 → 行为=现状（不查/不写缓存）。
    - 开关开且 chunk_ids 长度 == texts 长度：按 chunk_id 查 Redis
      （chunk_embed:{provider}:{chunk_id}），命中跳过 embed，miss 才 embed + 存。
    - chunk_id 为空串 / chunk_ids=None / 长度不匹配 → 走现状 embed（不缓存，不报错）。
    - 缓存层异常（Redis down / JSON 解析失败）静默降级，不阻塞 embed。
    - 缓存键含 provider（云/bge 向量空间不同，不混）。

    向后兼容：旧 caller embed_texts(texts) / embed_texts(["a","b"]) 零改动。
    """
    import time

    provider = settings.EMB_PROVIDER
    await ensure_model_consistency(provider)   # H1：向量空间漂移守卫（进程内缓存）
    cache_on = bool(getattr(settings, "EMBED_CHUNK_CACHE_ENABLE", False))
    # 严格对齐：长度不匹配 / 空 → 不启用缓存（防御 caller 误传）
    use_cache = (
        cache_on
        and chunk_ids is not None
        and len(chunk_ids) == len(texts)
    )

    # 1. 命中查询：从缓存读取已有向量，记录哪些 idx 需要 embed
    vecs: list[list[float] | None] = [None] * len(texts)
    miss_idx: list[int] = []
    if use_cache:
        r = redis_client.get_redis()
        for i, cid in enumerate(chunk_ids or []):
            if not cid:           # 空 chunk_id → 不查不存（chunk_id 缺失走现状 embed）
                miss_idx.append(i)
                continue
            try:
                cached = await r.get(f"chunk_embed:{provider}:{cid}")
            except Exception as e:
                degraded("chunk_embed_cache_get", e)
                cached = None     # 降级：当作 miss
            if cached is not None:
                try:
                    vecs[i] = json.loads(cached)
                    continue
                except Exception:
                    vecs[i] = None  # JSON 损坏 → 当作 miss
            miss_idx.append(i)
    else:
        miss_idx = list(range(len(texts)))

    # 2. miss 部分调底层 embed
    if miss_idx:
        miss_texts = [texts[i] for i in miss_idx]
        _t0 = time.time()
        new_vecs = await get_embedding_provider(provider).embed(miss_texts)
        try:
            metrics.EMBED_CALLS.labels(provider).inc(len(new_vecs))
            metrics.EMBED_LATENCY.labels(provider).observe(time.time() - _t0)
        except Exception:
            pass
        for j, i in enumerate(miss_idx):
            vecs[i] = new_vecs[j]
        # 3. miss 部分写缓存（仅 use_cache 时；写失败静默降级）
        if use_cache:
            ttl = getattr(settings, "EMBED_CHUNK_CACHE_TTL", 604800)
            for j, i in enumerate(miss_idx):
                cid = (chunk_ids or [""])[i]
                if not cid or vecs[i] is None:
                    continue
                try:
                    await r.set(
                        f"chunk_embed:{provider}:{cid}",
                        json.dumps(vecs[i]),
                        ex=ttl,
                    )
                except Exception as e:
                    degraded("chunk_embed_cache_set", e)

    # 全部命中时 vecs 已填满；texts 空 → vecs 空
    return [v for v in vecs]  # type: ignore[list-item]


async def embed_query(text: str, provider: str | None = None) -> list[float]:
    """单条 query 向量，带 Redis 缓存（高频/重复问题省 embedding 调用）。

    B3：命中时可选滑动续期（EMBED_CACHE_SLIDE_TTL_ENABLE，默认关）。
    开关关 → 现状（固定 TTL 自然过期）；开关开 → 命中续期，高频 query 保活。
    """
    p = provider or settings.EMB_PROVIDER
    await ensure_model_consistency(p)   # H1：向量空间漂移守卫（进程内缓存，仅首次查 Redis）
    key = f"emb:{p}:{text}"
    r = redis_client.get_redis()
    try:
        cached = await r.get(key)
        if cached:
            # B3：命中续期（高频 query 保活，默认关）
            if getattr(settings, "EMBED_CACHE_SLIDE_TTL_ENABLE", False):
                try:
                    await r.expire(key, settings.EMBED_CACHE_TTL)
                except Exception:
                    pass
            return json.loads(cached)
    except Exception as e:
        degraded("embed_cache_get", e)
    import time
    _t0 = time.time()
    vec = (await get_embedding_provider(p).embed([text]))[0]
    try:
        metrics.EMBED_CALLS.labels(p).inc()
        metrics.EMBED_LATENCY.labels(p).observe(time.time() - _t0)
    except Exception:
        pass
    try:
        await r.set(key, json.dumps(vec), ex=getattr(settings, "EMBED_CACHE_TTL", 3600))
    except Exception as e:
        degraded("embed_cache_set", e)
    return vec
