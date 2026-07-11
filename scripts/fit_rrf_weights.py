"""离线拟合 RRF 融合权重：用 backend/data/golden_qa.json 回归集网格搜索 dense/sparse 权重 + k。

判定：top-K 命中 relevant_docs(文档名) 或 chunk 文本含 expect 关键词 → 命中。
指标：recall@K（主） + DCG（辅，用 relevant_docs 分级做 gain）。
网格：dense 固定 1.0，搜 sparse 倍率 × {0.5,0.8,1.0,1.2,1.5,2.0} 与 k × {30,60,100}。

运行（需 DB/Milvus/Redis 已起，后端环境）：
    PYTHONPATH=backend python scripts/fit_rrf_weights.py

输出最优组合的建议 .env 值——人工确认后再落配置，不自动覆盖。
评估期间临时关闭改写/HyDE/多查询（纯评 RRF 检索召回）。
"""
import asyncio
import json
import sys
from pathlib import Path

# 让脚本能 import app.*
_BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(_BACKEND))

from app.config import settings  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.services import retrieval_service  # noqa: E402

GOLDEN_PATH = _BACKEND / "data" / "golden_qa.json"
TOPK = 5


def load_golden() -> list[dict]:
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        return json.load(f)


def score_hits(hits: list[dict], item: dict, k: int = TOPK) -> tuple[float, float]:
    """返回 (hit@k ∈ {0,1}, dcg)。命中 = docName 在 relevant_docs 或 chunk/doc 含 expect 词。"""
    rel_docs = item.get("relevant_docs") or {}
    expects = [e for e in (item.get("expect") or []) if e]
    hit_docs, dcg = set(), 0.0
    for rank, h in enumerate(hits[:k]):
        doc = h.get("docName", "") or ""
        chunk = h.get("chunk", "") or ""
        is_rel = doc in rel_docs or any(e in chunk for e in expects) or any(e in doc for e in expects)
        if is_rel:
            hit_docs.add(doc)
            grade = rel_docs.get(doc, 1)  # 无分级则 gain=1
            dcg += (2 ** grade - 1) / (2 ** (rank + 1))  # rank 从 0 → 位置 rank+1
    return (1.0 if hit_docs else 0.0), dcg


async def eval_combo(golden: list[dict], dense_w: float, sparse_w: float, rrf_k: int) -> tuple[float, float]:
    setattr(settings, "RRF_DENSE_WEIGHT", dense_w)
    setattr(settings, "RRF_SPARSE_WEIGHT", sparse_w)
    setattr(settings, "RRF_K", rrf_k)
    hit_sum, dcg_sum = 0.0, 0.0
    async with AsyncSessionLocal() as db:
        for item in golden:
            q = item.get("query", "")
            try:
                hits = await retrieval_service.mixed_search(db, q, TOPK)
            except Exception as e:
                print(f"  [warn] '{q}' 检索失败: {e}")
                continue
            hit, dcg = score_hits(hits, item, TOPK)
            hit_sum += hit
            dcg_sum += dcg
    n = len(golden) or 1
    return hit_sum / n, dcg_sum / n


async def main() -> None:
    # 评估期关闭会触发 LLM 的改写链路，纯评 RRF 检索召回
    for flag in ("QUERY_REWRITE_ENABLE", "MULTI_QUERY_ENABLE", "HYDE_ENABLE"):
        setattr(settings, flag, False)

    golden = load_golden()
    print(f"[fit_rrf] golden 集 {len(golden)} 条，网格搜索 (topK={TOPK})…")

    grid = [(1.0, sw, k) for sw in (0.5, 0.8, 1.0, 1.2, 1.5, 2.0) for k in (30, 60, 100)]
    best = None  # (recall, dcg, dense, sparse, k)
    print(f"{'dense':>6} {'sparse':>6} {'k':>4} {'recall':>8} {'dcg':>8}")
    for dense_w, sparse_w, rrf_k in grid:
        recall, dcg = await eval_combo(golden, dense_w, sparse_w, rrf_k)
        print(f"{dense_w:>6} {sparse_w:>6} {rrf_k:>4} {recall:>8.3f} {dcg:>8.3f}")
        if best is None or recall > best[0] + 1e-6 or (abs(recall - best[0]) <= 1e-6 and dcg > best[1]):
            best = (recall, dcg, dense_w, sparse_w, rrf_k)

    print("\n[fit_rrf] 最优：recall=%.3f dcg=%.3f" % (best[0], best[1]))
    print("[fit_rrf] 建议 .env（人工确认后落配置，不自动覆盖）：")
    print(f"  RRF_DENSE_WEIGHT={best[2]}")
    print(f"  RRF_SPARSE_WEIGHT={best[3]}")
    print(f"  RRF_K={best[4]}")


if __name__ == "__main__":
    asyncio.run(main())
