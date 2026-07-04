"""检索质量回归评测：Recall/Precision/F1@K / MRR / NDCG@K / 空结果率 + 分类报告 + 阈值门禁。

数据集：backend/data/golden_qa.json（query + 期望命中文档关键词 expect[] + 可选 graded relevant_docs + 分类）。
运行（需先 seed_demo 建库 + 后端在 8001）:
    python scripts/eval_retrieval.py [--topk 5] [--threshold 0.85]

输出：控制台报告 + reports/eval_retrieval_<时间戳>.md；recall 低于阈值时退出码 1（CI 门禁）。
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8001"
GOLDEN = Path(__file__).resolve().parent.parent / "backend" / "data" / "golden_qa.json"


def load_golden():
    items = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert items and isinstance(items, list), "golden_qa.json 为空或格式错"
    return items


def doc_relevance_binary(expect: list[str], doc_name: str) -> int:
    """二元相关性：任一 expect 关键词出现在 docName 中 → 1，否则 0。"""
    return 1 if any(kw in doc_name for kw in expect) else 0


def doc_relevance_graded(relevant_docs: dict, doc_name: str, fallback_expect: list[str]) -> int:
    """分级相关性：优先用 relevant_docs{docName→grade}，否则 fallback 到二元。
    grade: 3=高度相关(标题精确匹配), 2=相关(内容覆盖), 1=部分相关(侧面提及), 0=无关。
    """
    if relevant_docs:
        # 用 docName 子串匹配 relevant_docs 的 key
        for key, grade in relevant_docs.items():
            if key in doc_name:
                return grade
        return 0  # 不在 relevant_docs 中 → 无关
    return doc_relevance_binary(fallback_expect, doc_name)


def hit_rank_graded(relevant_docs: dict, expect: list[str], docs: list[str]):
    """返回首个命中的排名(1-based)及等级；未命中返回 None,0。"""
    for rank, d in enumerate(docs, 1):
        grade = doc_relevance_graded(relevant_docs, d, expect)
        if grade > 0:
            return rank, grade
    return None, 0


def ndcg_at_k(docs: list[str], relevant_docs: dict, expect: list[str], k: int) -> float:
    """NDCG@K：分级相关度 + 对数位置折损。
    如果 relevant_docs 为空，退化为二元 NDCG（相关=1，不相关=0）。
    """
    rels = [doc_relevance_graded(relevant_docs, d, expect) for d in docs[:k]]
    dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rels))  # i+2 because i is 0-based
    # IDCG: 理想排序（所有 relevant_docs 中 grade>0 的按等级降序排前面）
    if relevant_docs:
        ideal = sorted(relevant_docs.values(), reverse=True)[:k]
    else:
        # 用 expect 数量作为理论上能达到的最大相关文档数
        max_rel = min(len(expect), k)
        ideal = [1] * max_rel + [0] * (k - max_rel)
    idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def precision_at_k(docs: list[str], relevant_docs: dict, expect: list[str], k: int) -> float:
    """Precision@K：top-K 中相关文档占比。"""
    if k == 0 or not docs:
        return 0.0
    rels = [doc_relevance_graded(relevant_docs, d, expect) for d in docs[:k]]
    return sum(1 for r in rels if r > 0) / min(k, len(docs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--threshold", type=float, default=0.85, help="recall 门禁阈值(低于则 exit 1)")
    args = ap.parse_args()

    golden = load_golden()
    with httpx.Client(timeout=60) as c:
        token = c.post(
            f"{BASE}/api/system/login", json={"username": "admin", "password": "admin123"}
        ).json()["data"]["token"]
        H = {"Authorization": "Bearer " + token}

        rows, by_cat = [], {}
        n_hit = n_empty = 0
        mrr_sum = 0.0
        ndcg_sum = 0.0
        precision_sum = 0.0
        for it in golden:
            q = it["query"]
            expect = it.get("expect", [])
            relevant_docs = it.get("relevant_docs", {})
            cat = it.get("category", "未分类")
            r = c.post(f"{BASE}/api/retrieval/mixed", headers=H,
                       json={"query": q, "topK": args.topk}).json()
            docs = [x["docName"] for x in r.get("data", {}).get("retrievalList", [])]
            rank = hit_rank_graded(relevant_docs, expect, docs)[0]
            ok = rank is not None
            n_hit += ok
            n_empty += (len(docs) == 0)
            mrr_sum += (1.0 / rank) if rank else 0.0
            p_k = precision_at_k(docs, relevant_docs, expect, args.topk)
            precision_sum += p_k
            n_val = ndcg_at_k(docs, relevant_docs, expect, args.topk)
            ndcg_sum += n_val
            by_cat.setdefault(cat, {"hits": 0, "total": 0, "precision_sum": 0.0, "ndcg_sum": 0.0, "mrr_sum": 0.0})
            by_cat[cat]["hits"] += ok
            by_cat[cat]["total"] += 1
            by_cat[cat]["precision_sum"] += p_k
            by_cat[cat]["ndcg_sum"] += n_val
            by_cat[cat]["mrr_sum"] += (1.0 / rank) if rank else 0.0
            rows.append((cat, q, expect, rank, ok, docs, p_k, n_val))

    n = len(golden)
    recall = n_hit / n
    mrr = mrr_sum / n
    ndcg = ndcg_sum / n
    precision = precision_sum / n
    f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
    empty_rate = n_empty / n
    passed = recall >= args.threshold

    # 控制台报告
    topk = args.topk
    print(f"\n===== 检索质量评测 recall@{topk} =====")
    print(f"样本数: {n}  |  召回率: {recall*100:.1f}%  |  精确率@K: {precision*100:.1f}%")
    print(f"F1@K: {f1:.3f}  |  MRR: {mrr:.3f}  |  NDCG@K: {ndcg:.3f}  |  无结果率: {empty_rate*100:.1f}%")
    print(f"门禁阈值: {args.threshold*100:.0f}%  ->  {'✓ PASS' if passed else '✗ FAIL'}\n")
    print(f"{'分类':<8} {'命中/总数':<12} {'召回率':<10} {'精确率':<10} {'NDCG':<8} {'MRR':<8}")
    print("-" * 56)
    for cat, stats in by_cat.items():
        t = stats["total"]
        print(f"  {cat:<6} {stats['hits']}/{t:<9} {stats['hits']/t*100:>6.0f}%    "
              f"{stats['precision_sum']/t*100:>6.0f}%     {stats['ndcg_sum']/t:>5.3f}   {stats['mrr_sum']/t:>5.3f}")
    print("\n明细:")
    for cat, q, expect, rank, ok, docs, p_k, n_val in rows:
        mark = "✓" if ok else "✗"
        rstr = f"@{rank}" if rank else "未命中"
        print(f"  {mark} [{cat}] {q[:32]:<32} 期望{expect} {rstr}  P@{topk}={p_k:.1f}  NDCG={n_val:.3f}")

    # markdown 报告
    rep = Path("reports")
    rep.mkdir(exist_ok=True)
    md = rep / f"eval_retrieval_{int(time.time())}.md"
    lines = [f"# 检索质量评测 recall@{topk}", "",
             f"- 样本数: **{n}**",
             f"- 召回率: **{recall*100:.1f}%**",
             f"- 精确率@K: **{precision*100:.1f}%**",
             f"- F1@K: **{f1:.3f}**",
             f"- MRR: **{mrr:.3f}**",
             f"- NDCG@K: **{ndcg:.3f}**",
             f"- 无结果率: **{empty_rate*100:.1f}%**",
             f"- 门禁({args.threshold*100:.0f}%): {'✓ PASS' if passed else '✗ FAIL'}", "",
             "| 分类 | 命中/总数 | 召回率 | 精确率@K | NDCG@K | MRR |",
             "|---|---|---|---|---|"]
    for cat, stats in by_cat.items():
        t = stats["total"]
        lines.append(
            f"| {cat} | {stats['hits']}/{t} | {stats['hits']/t*100:.0f}% | "
            f"{stats['precision_sum']/t*100:.0f}% | {stats['ndcg_sum']/t:.3f} | {stats['mrr_sum']/t:.3f} |")
    lines += ["", "## 失败 case", ""]
    fails = [r for r in rows if not r[4]]
    if not fails:
        lines.append("无")
    for cat, q, expect, rank, ok, docs, p_k, n_val in fails:
        lines.append(f"- [{cat}] **{q}** 期望 {expect}，P@K={p_k:.1f} NDCG={n_val:.3f}，实际 {docs[:3]}")
    md.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写: {md}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
