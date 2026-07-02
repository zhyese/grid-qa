"""预热 + 持久化 Redis 常见问题缓存。

把 golden 常见问题按 qa_service 同一套 key（_cache_key(model, term_normalize(query))）
写进 Redis，**无 TTL 永久保存**（cache_set_json_persistent），不受 QA_CACHE_TTL(1h) 淘汰。
- 已缓存(含TTL)的条目：直接转持久（剥离 TTL，保留原值，免 LLM）。
- 未命中/过期：走真实 answer() 生成再持久化。

跑法（从 repo 根；:8001 不依赖，脚本自连 DB/Redis/Provider）：
  venv/Scripts/python.exe prewarm_cache.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from app.clients import redis_client
from app.db.session import AsyncSessionLocal
from app.services import qa_service, term_service


async def persist_one(db, q):
    """返回 (key, val, source, ttl)。source: existing=原缓存转持久 | generated=新生成。"""
    nq = term_service.normalize(q)
    key = qa_service._cache_key(None, nq)  # 与 answer()/stream_answer() 内部完全一致
    val = await redis_client.cache_get_json(key)
    source = "existing"
    if not val:
        val = await qa_service.answer(
            db, q, None, conversation_id=None, username="admin", tenant="default"
        )
        source = "generated"
    await redis_client.cache_set_json_persistent(key, val)
    ttl = await redis_client.get_redis().ttl(key)  # -1=永久无过期
    return key, val, source, ttl


async def main():
    golden = json.load(open("backend/data/golden_qa.json", encoding="utf-8"))
    queries = [g["query"] for g in golden] + [
        "主变压器温度异常如何处置？",
        "配电线路单相接地故障如何排查？",
        "SF6断路器漏气该如何处理？",
        "变压器日常巡视检查哪些项目？",
    ]
    queries = list(dict.fromkeys(queries))
    print(f"persisting {len(queries)} common questions (strip TTL -> permanent)...")
    done = 0
    async with AsyncSessionLocal() as db:
        for i, q in enumerate(queries, 1):
            try:
                _key, val, source, ttl = await persist_one(db, q)
                print(
                    f"[{i:>2}/{len(queries)}] src={source:9} ttl={ttl:>3} "
                    f"len={len(val.get('answer', ''))} | {q[:24]}"
                )
                if ttl == -1:
                    done += 1
            except Exception as e:
                print(f"[{i:>2}] FAIL {type(e).__name__}: {e}")
    print(f"done. permanent(ttl=-1): {done}/{len(queries)}")


if __name__ == "__main__":
    asyncio.run(main())
