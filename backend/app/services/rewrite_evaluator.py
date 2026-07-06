"""改写质量评估：改写前后各跑一次轻量 dense 检索，比 top-K 分数和。

底层逻辑：直接跑全链路检索评估太重（双倍 rerank/MMR 成本），这里只跑单路 dense_cloud
（cand=10）算 top-K 分数和——信号粗但够判改写是否更优，延迟可控（~50ms×2 并发）。
rewritten 分数和 > original*(1+REWRITE_EVAL_MARGIN) 才算更优（margin 防抖动误判）。
异常回退 not improved（用原 query，安全）。
"""
import asyncio

from app.clients import milvus_client
from app.config import settings
from app.core.obs import degraded
from app.services import embedding_service


async def _light_dense(query: str, model_type: str | None) -> list[dict]:
    """单路 dense_cloud 轻量检索，返回 [{score, ...}, ...]。"""
    qvec = await embedding_service.embed_query(query, settings.EMB_PROVIDER)
    return await asyncio.to_thread(
        milvus_client.search, settings.MILVUS_COLLECTION, qvec, settings.REWRITE_EVAL_CAND,
    )


def _score_sum(hits: list[dict]) -> float:
    """top-K 分数和（K=REWRITE_EVAL_TOPK）。"""
    k = settings.REWRITE_EVAL_TOPK
    return sum(float(h.get("score", 0) or 0) for h in (hits or [])[:k])


async def evaluate(original: str, rewritten: str, model_type: str | None) -> dict:
    """对比改写前后检索分数，返回 {improved, orig_score, new_score}。异常回退 not improved。"""
    try:
        orig_hits, new_hits = await asyncio.gather(
            _light_dense(original, model_type),
            _light_dense(rewritten, model_type),
        )
        orig_s, new_s = _score_sum(orig_hits), _score_sum(new_hits)
        improved = new_s > orig_s * (1 + settings.REWRITE_EVAL_MARGIN)
        return {"improved": improved, "orig_score": round(orig_s, 4), "new_score": round(new_s, 4)}
    except Exception as e:
        degraded("rewrite_eval", e)
        return {"improved": False, "orig_score": 0.0, "new_score": 0.0}
