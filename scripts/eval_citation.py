# scripts/eval_citation.py
"""引用评测：四样本 × 四指标 + CI 门禁。

四指标：
  - 引用覆盖 coverage：答案关键事实绑定的 ref 占 expect_refs 比例
  - 证据关联率 association：经校验 nli_label=support 的 ref 占比
  - 证据完整度 completeness：多证据样本集齐 expect_refs 的比例
  - 事实一致性 consistency：高风险样本无篡改（contradict=0）
关联率 < CITATION_ASSOCIATION_GATE(0.8) → 退出码 1（CI 门禁）。

两档执行：
  默认（本地有云 key）：跑三层校验含 NLI（nli_enable=True）。
  --no-nli（PR CI 门禁）：nli_enable=False，仅校验1+2，免云 key。

用法：
  python scripts/eval_citation.py [--gate 0.8] [--no-nli]
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.rag.citation_index import build_index
from app.schemas.citation import parse_citation_answer
from app.rag.citation_verifier import verify

GOLDEN = Path(__file__).resolve().parent.parent / "backend" / "data" / "golden_citation.json"


async def evaluate(gate: float, nli_enable: bool) -> dict:
    samples = json.loads(GOLDEN.read_text(encoding="utf-8"))
    report: dict = {"samples": [], "metrics": {}}
    total_cov, total_assoc, total_complete, total_consist = 0.0, 0.0, 0.0, 0.0
    n = len(samples)
    for s in samples:
        index = build_index(s["contexts"])
        parsed = parse_citation_answer(s["answer"], index, s["contexts"])
        verdict = await verify(parsed.answer_text, parsed.citation_map, index,
                               s["contexts"], None, nli_enable=nli_enable)
        refs = {it.ref_id for it in verdict.items if it.action == "keep"}
        expect = set(s.get("expect_refs", []))
        cov = len(refs & expect) / max(len(expect), 1) if expect else 1.0
        supports = [i for i in verdict.items if i.nli_label == "support"]
        # 关联率：无 NLI（--no-nli 或 degraded）时，keep 项视为通过校验1+2，按 keep 占比退化计量
        if nli_enable:
            denom = max(len(verdict.items), 1)
            assoc = len(supports) / denom if verdict.items else 0.0
        else:
            denom = max(len(verdict.items), 1)
            assoc = len([i for i in verdict.items if i.action == "keep"]) / denom if verdict.items else 0.0
        complete = 1.0 if expect.issubset(refs) else 0.0
        consist = 0.0 if any(i.nli_label == "contradict" for i in verdict.items) else 1.0
        total_cov += cov
        total_assoc += assoc
        total_complete += complete
        total_consist += consist
        report["samples"].append({
            "id": s["id"], "category": s["category"],
            "coverage": round(cov, 3), "association": round(assoc, 3),
            "completeness": complete, "consistency": consist,
            "degraded": verdict.degraded,
        })
    passed = (total_assoc / n) >= gate
    report["metrics"] = {
        "coverage": round(total_cov / n, 3),
        "association": round(total_assoc / n, 3),
        "completeness": round(total_complete / n, 3),
        "consistency": round(total_consist / n, 3),
        "gate": gate,
        "nli_enable": nli_enable,
        "pass": passed,
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="引用评测 + CI 门禁")
    ap.add_argument("--gate", type=float, default=0.8,
                    help="关联率门禁阈值，低于则退出码 1（默认 0.8）")
    ap.add_argument("--no-nli", action="store_true",
                    help="跳过 NLI 校验（仅校验1 格式 + 校验2 向量），PR CI 免云 key 档")
    args = ap.parse_args()
    rep = asyncio.run(evaluate(args.gate, nli_enable=not args.no_nli))
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if rep["metrics"]["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
