"""检索评测 service：服务化 eval_retrieval，直接调 mixed_search（不绕 HTTP），算 recall/MRR/nDCG。

只建议模式的评测基座：扫描引擎（retrieval_tune_service）用本服务跑 golden 集，
对比不同 overrides 的 recall/MRR/nDCG/无结果率，产出调参建议。
"""
import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import retrieval_service

_GOLDEN = Path(__file__).resolve().parent.parent.parent / "data" / "golden_qa.json"


def _load_golden() -> list[dict]:
    """加载 golden 问答集（backend/data/golden_qa.json）。"""
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


def _recall_at_k(expect: list[str], got: list[str]) -> float:
    """recall@k：期望文档命中比例（二值相关）。"""
    if not expect:
        return 0.0
    hit = sum(1 for d in expect if d in got)
    return hit / len(expect)


def _mrr(expect: list[str], got: list[str]) -> float:
    """MRR：第一个命中的期望文档的倒数排名。"""
    for i, d in enumerate(got, 1):
        if d in expect:
            return 1.0 / i
    return 0.0


def _ndcg(relevant_docs: dict, got: list[str]) -> float:
    """分级 nDCG（relevant_docs value 1-3 为相关性等级）。"""
    def _dcg(order):
        s = 0.0
        for i, d in enumerate(order, 1):
            rel = relevant_docs.get(d, 0)
            if rel:
                s += (2 ** rel - 1) / (i + 1)
        return s
    ideal = sorted(relevant_docs.values(), reverse=True)
    idcg = sum((2 ** r - 1) / (i + 1) for i, r in enumerate(ideal, 1))
    if idcg == 0:
        return 0.0
    return _dcg(got) / idcg


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 4) if xs else 0.0


async def evaluate_over_golden(db: AsyncSession, overrides: dict | None = None, topk: int = 5) -> dict:
    """跑 golden 集，返回 {recall, mrr, ndcg, noResultRate, sampleSize, validSample, perQuery}。

    overrides: 调参扫描时传 {RRF_K:40,...}；None=走 settings（baseline）。
    """
    golden = _load_golden()
    recalls, mrrs, ndcgs, n_empty = [], [], [], 0
    per_query = []
    for item in golden:
        ctx = await retrieval_service.mixed_search(db, item["query"], topk, overrides=overrides)
        got = [c["docName"] for c in ctx] if ctx else []
        if not ctx:
            n_empty += 1
            per_query.append({"query": item["query"], "recall": 0.0, "mrr": 0.0, "empty": True})
            continue
        r = _recall_at_k(item.get("expect", []), got)
        m = _mrr(item.get("expect", []), got)
        recalls.append(r)
        mrrs.append(m)
        n = None
        if item.get("relevant_docs"):
            n = _ndcg(item.get("relevant_docs", {}), got)
            ndcgs.append(n)
        per_query.append({"query": item["query"], "recall": r, "mrr": m, "ndcg": n})
    return {
        "recall": _mean(recalls), "mrr": _mean(mrrs), "ndcg": _mean(ndcgs),
        "noResultRate": round(n_empty / len(golden), 4) if golden else 0.0,
        "sampleSize": len(golden), "validSample": len(recalls), "perQuery": per_query,
    }
