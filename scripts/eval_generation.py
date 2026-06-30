"""生成质量评测：端到端 faithfulness（LLM-judge 答案支撑率）+ 门禁（S1）。

对 golden_qa.json 每条：1) POST /qa/answer 拿答案 2) judge_hallucination 判定支撑率。
平均 supported_ratio < FAITHFULNESS_GATE(默认 0.85) → exit 1（CI 深度门禁）。

与 eval_retrieval 互补：retrieval 测"找得准不准"（recall），generation 测"答得可信不可信"
（faithfulness/真实幻觉）。需后端运行 + LLM，按需手动/夜间跑。

运行: python scripts/eval_generation.py [--limit 10] [--gate 0.85]
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, "backend")
BASE = "http://127.0.0.1:8001"
GOLDEN = Path(__file__).resolve().parent.parent / "backend" / "data" / "golden_qa.json"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10, help="评测条数（每条 1 次 LLM，控制成本）")
    ap.add_argument("--gate", type=float, default=0.85, help="平均支撑率门禁（低于 exit 1）")
    args = ap.parse_args()

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))[: args.limit]
    print(f"===== 生成质量评测 faithfulness（{len(golden)} 条）=====")

    async with httpx.AsyncClient(timeout=120) as c:
        token = (
            await c.post(f"{BASE}/api/system/login", json={"username": "admin", "password": "admin123"})
        ).json()["data"]["token"]
        H = {"Authorization": "Bearer " + token}
        from app.config import settings
        from app.rag.judge import judge_hallucination

        rows, sup_sum = [], 0.0
        for it in golden:
            q = it["query"]
            r = (
                await c.post(f"{BASE}/api/qa/answer", headers=H, json={"query": q, "modelType": "deepseek"})
            ).json()["data"]
            sources = [s.get("text", "") for s in r.get("retrievalSource", [])]
            j = await judge_hallucination(r["answer"], sources, settings.LLM_PROVIDER)
            sup_sum += j["supported_ratio"]
            rows.append((q, j["supported_ratio"], j["hallucination"]))
            print(f'  支撑={j["supported_ratio"]:.2f} 幻觉={j["hallucination"]:.2f} | {q[:24]}')

    n = len(rows)
    avg_sup = sup_sum / n if n else 0.0
    avg_halluc = sum(r[2] for r in rows) / n if n else 0.0
    passed = avg_sup >= args.gate
    print(f"\n平均支撑率 = {avg_sup:.2%} | 平均幻觉率 = {avg_halluc:.2%}")
    print(f"门禁 {args.gate:.0%} → {'✓ PASS' if passed else '✗ FAIL'}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
