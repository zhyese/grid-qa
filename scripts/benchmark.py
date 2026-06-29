"""并发压测检索接口（embedding + Milvus，不调 LLM，可承并发）。

运行: python scripts/benchmark.py [并发数]
默认 50 并发。问答接口因含 LLM 调用受 DeepSeek 速率限制，单独测小并发。
"""
import asyncio
import sys
import time

import httpx

BASE = "http://127.0.0.1:8001"
QUERIES = ["主变压器巡视", "断路器维护", "线路保护", "互感器运行", "隔离开关操作", "变压器故障"]


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    async with httpx.AsyncClient(timeout=60) as c:
        token = (await c.post(
            f"{BASE}/api/system/login",
            json={"username": "admin", "password": "admin123"},
        )).json()["data"]["token"]
        H = {"Authorization": "Bearer " + token}

        async def one(i):
            try:
                r = await c.post(
                    f"{BASE}/api/retrieval/mixed", headers=H,
                    json={"query": QUERIES[i % len(QUERIES)], "topK": 5},
                )
                return r.status_code == 200, r.elapsed.total_seconds()
            except Exception:
                return False, 0.0

        t0 = time.time()
        results = await asyncio.gather(*[one(i) for i in range(n)])
        dt = time.time() - t0

    ok = sum(1 for s, _ in results if s)
    lats = sorted(t for _, t in results if t)
    p50 = lats[len(lats) // 2] if lats else 0
    p95 = lats[int(len(lats) * 0.95)] if lats else 0
    print(f"并发 {n}: 成功 {ok}/{n} | 总耗时 {dt:.2f}s | 吞吐 {ok / dt:.1f} req/s | P50 {p50:.2f}s | P95 {p95:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
