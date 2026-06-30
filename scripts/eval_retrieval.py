"""检索质量回归评测：召回率 recall@K / MRR / 无结果率 + 分类报告 + 阈值门禁。

数据集：backend/data/golden_qa.json（query + 期望命中文档关键词 expect[] + 分类）。
运行（需先 seed_demo 建库 + 后端在 8001）:
    python scripts/eval_retrieval.py [--topk 5] [--threshold 0.92]

输出：控制台报告 + reports/eval_retrieval_<时间戳>.md；recall 低于阈值时退出码 1（CI 门禁）。
"""
import argparse
import json
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


def hit_rank(expect: list[str], docs: list[str]):
    """返回首个命中的排名(1-based)；未命中返回 None。任一 expect 关键词出现在 docName 即命中。"""
    for rank, d in enumerate(docs, 1):
        if any(kw in d for kw in expect):
            return rank
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--threshold", type=float, default=0.92, help="recall 门禁阈值(低于则 exit 1)")
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
        for it in golden:
            q, expect, cat = it["query"], it["expect"], it.get("category", "未分类")
            r = c.post(f"{BASE}/api/retrieval/mixed", headers=H,
                       json={"query": q, "topK": args.topk}).json()
            docs = [x["docName"] for x in r.get("data", {}).get("retrievalList", [])]
            rank = hit_rank(expect, docs)
            ok = rank is not None
            n_hit += ok
            n_empty += (len(docs) == 0)
            mrr_sum += (1.0 / rank) if rank else 0.0
            by_cat.setdefault(cat, [0, 0])
            by_cat[cat][0] += ok
            by_cat[cat][1] += 1
            rows.append((cat, q, expect, rank, ok, docs))

    n = len(golden)
    recall = n_hit / n
    mrr = mrr_sum / n
    empty_rate = n_empty / n
    passed = recall >= args.threshold

    # 控制台报告
    print(f"\n===== 检索质量评测 recall@{args.topk} =====")
    print(f"样本数: {n}  | 召回率: {recall*100:.1f}%  | MRR: {mrr:.3f}  | 无结果率: {empty_rate*100:.1f}%")
    print(f"门禁阈值: {args.threshold*100:.0f}%  ->  {'✓ PASS' if passed else '✗ FAIL'}\n")
    print("分类召回:")
    for cat, (h, t) in by_cat.items():
        print(f"  {cat}: {h}/{t} = {h/t*100:.0f}%")
    print("\n明细:")
    for cat, q, expect, rank, ok, docs in rows:
        mark = "✓" if ok else "✗"
        rstr = f"@{rank}" if rank else "未命中"
        print(f"  {mark} [{cat}] {q[:24]:<24} 期望{expect} {rstr}")

    # markdown 报告
    rep = Path("reports")
    rep.mkdir(exist_ok=True)
    md = rep / f"eval_retrieval_{int(time.time())}.md"
    lines = [f"# 检索质量评测 recall@{args.topk}", "",
             f"- 样本数: **{n}**", f"- 召回率: **{recall*100:.1f}%**",
             f"- MRR: **{mrr:.3f}**", f"- 无结果率: **{empty_rate*100:.1f}%**",
             f"- 门禁({args.threshold*100:.0f}%): {'✓ PASS' if passed else '✗ FAIL'}", "",
             "| 分类 | 命中/总数 | 召回率 |", "|---|---|---|"]
    for cat, (h, t) in by_cat.items():
        lines.append(f"| {cat} | {h}/{t} | {h/t*100:.0f}% |")
    lines += ["", "## 失败 case", ""]
    fails = [r for r in rows if not r[4]]
    if not fails:
        lines.append("无")
    for cat, q, expect, rank, ok, docs in fails:
        lines.append(f"- [{cat}] **{q}** 期望 {expect}，实际 {docs[:3]}")
    md.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写: {md}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
