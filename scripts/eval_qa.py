"""LLM-as-judge 问答评测：对问答集算真实准确率/幻觉率。

运行: python scripts/eval_qa.py   (后端需运行)
每个问题: 1) 调 /api/qa/answer 拿答案 2) 调 judge 判定支撑率。
"""
import asyncio
import sys

import httpx

sys.path.insert(0, "backend")
BASE = "http://127.0.0.1:8001"

QUERIES = [
    "主变压器温度异常如何处置",
    "SF6断路器气体压力偏低怎么办",
    "隔离开关能不能带负荷操作",
    "电流互感器二次侧为什么不能开路",
    "输电线路的主保护有哪些",
    "配电变压器套管闪络怎么处理",
]


async def main():
    async with httpx.AsyncClient(timeout=120) as c:
        token = (await c.post(
            f"{BASE}/api/system/login",
            json={"username": "admin", "password": "admin123"},
        )).json()["data"]["token"]
        H = {"Authorization": "Bearer " + token}

        from app.rag.judge import judge_hallucination

        total_halluc = 0.0
        for q in QUERIES:
            r = (await c.post(
                f"{BASE}/api/qa/answer", headers=H,
                json={"query": q, "modelType": "deepseek"},
            )).json()["data"]
            j = await judge_hallucination(r["answer"], r["retrievalSource"])
            total_halluc += j["hallucination"]
            print(f'  {q[:18]:<20} 支撑={j["supported_ratio"]:.2f} 幻觉={j["hallucination"]:.2f} | {j["reason"][:28]}')

        avg = total_halluc / len(QUERIES)
        print(f"\n平均幻觉率 = {avg:.2%} (目标 ≤5%)")
        print(f"问答准确率 ≈ {(1 - avg):.0%} (LLM-as-judge)")


if __name__ == "__main__":
    asyncio.run(main())
