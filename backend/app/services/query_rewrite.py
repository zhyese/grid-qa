"""LLM query 改写：口语化/简短提问 → 规范完整检索查询，提升召回。

默认关闭（QUERY_REWRITE_ENABLE），开启会增加一次 LLM 调用延迟。
"""
from app.config import settings
from app.core.obs import degraded
from app.providers.factory import get_llm_provider
from app.services import rewrite_cache, rewrite_evaluator, rewrite_event_service
from app.services.rewrite_strategy import classify, get_fewshot


async def rewrite_query(query: str, model_type: str | None = None, force: bool = False) -> str:
    """改写失败时原样返回，不影响主流程。force=True 时跳过开关（CRAG 纠错强制改写用）。"""
    if (not force and not settings.QUERY_REWRITE_ENABLE) or not query.strip():
        return query
    prompt = (
        "你是电网运维检索查询改写助手。将下面的用户提问改写为更规范、信息更完整、"
        "适合向量检索的查询（保留关键设备/故障/操作术语，去掉口语）。"
        "只输出改写后的查询，不要解释、不要引号：\n" + query
    )
    try:
        return (await get_llm_provider(model_type).chat(
            [{"role": "user", "content": prompt}], temperature=0, max_tokens=120
        )).strip() or query
    except Exception as e:
        degraded("query_rewrite", e)
        return query


def _build_prompt(query: str, strategy: dict) -> str:
    """按策略类型带 few-shot 构造改写 prompt。"""
    fs = get_fewshot(strategy["type"])
    examples = "".join(f"示例：{e['q']} → {e['r']}\n" for e in fs)
    return (
        "你是电网运维检索查询改写助手。把下面提问改写为更规范、信息更完整、适合向量检索的查询"
        f"（{strategy['hint']}，保留关键设备/故障/操作术语，去掉口语）。只输出改写后查询，不要解释：\n"
        f"{examples}输入：{query}\n输出："
    )


async def rewrite_query_v2(query: str, model_type: str | None = None) -> dict:
    """完整改写闭环：Classifier→Cache→改写(带few-shot)→Evaluator→记事件。

    规范 query 被 Classifier 判 skip（adaptive 跳过，省 LLM+评估延迟）；
    缓存命中跳过 LLM；改写后评估更优才用改写，否则回退原 query。
    返回 {query, strategy, improved, cached, orig_score, new_score}。
    CRAG force 改写仍走旧 rewrite_query(force=True)，绕过本闭环。
    """
    strategy = classify(query)
    if strategy["skip"] and settings.REWRITE_ADAPTIVE_ENABLE:
        return {"query": query, "strategy": "normal", "improved": False,
                "cached": False, "orig_score": 0.0, "new_score": 0.0}
    # 缓存
    cached = await rewrite_cache.get(strategy["type"], query)
    if cached:
        try:
            from app.core import metrics
            metrics.REWRITE_CACHE_HIT.labels("rewrite").inc()
        except Exception:
            pass
        await rewrite_event_service.log("rewrite", query, cached.get("result", query),
                                        cached.get("improved", False), 0, 0, cached=True)
        return {"query": cached.get("result", query) if cached.get("improved") else query,
                "strategy": strategy["type"], "improved": cached.get("improved", False),
                "cached": True, "orig_score": 0.0, "new_score": 0.0}
    # LLM 改写（带 few-shot）
    rewritten = query
    try:
        rewritten = (await get_llm_provider(model_type).chat(
            [{"role": "user", "content": _build_prompt(query, strategy)}],
            temperature=0, max_tokens=120,
        )).strip() or query
    except Exception as e:
        degraded("query_rewrite_v2", e)
        rewritten = query
    # 评估
    improved, orig_s, new_s = False, 0.0, 0.0
    if settings.REWRITE_EVAL_ENABLE and rewritten != query:
        ev = await rewrite_evaluator.evaluate(query, rewritten, model_type)
        improved, orig_s, new_s = ev["improved"], ev["orig_score"], ev["new_score"]
    result = rewritten if improved else query
    # 写缓存 + 记事件 + 指标
    await rewrite_cache.set(strategy["type"], query, {"result": rewritten, "improved": improved})
    await rewrite_event_service.log("rewrite", query, rewritten, improved, orig_s, new_s, cached=False)
    try:
        from app.core import metrics
        (metrics.REWRITE_IMPROVED if improved else metrics.REWRITE_EVAL_REJECTED).labels("rewrite").inc()
    except Exception:
        pass
    return {"query": result, "strategy": strategy["type"], "improved": improved,
            "cached": False, "orig_score": orig_s, "new_score": new_s}
