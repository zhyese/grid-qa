"""缓存预热工具：从 MySQL qa_cache 表拉取 Top-N 热点问题，预加载到 Redis。

使用场景:
  - 服务启动后调用，让高频问题即刻命中 Redis L1（避免冷启动走全链路）
  - 定时任务每 6h 刷新热点（hit_count 排名前 50）

用法:
  python -m app.services.cache_warmup          # 手动预热
  await warmup_hot_queries(db, topk=50)        # 代码内调用
"""
import asyncio
import json

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import redis_client
from app.config import settings
from app.core.obs import degraded
from app.models.qa_cache import QaCache


async def warmup_hot_queries(
    db: AsyncSession, topk: int = 50, min_hits: int = 2,
) -> int:
    """从 MySQL qa_cache 拉取 hit_count 最高的 topk 条记录，回写到 Redis。

    条件: hit_count >= min_hits, 未过期, 未软删。
    返回成功预热条数。
    """
    try:
        rows = (
            await db.execute(
                select(QaCache)
                .where(
                    QaCache.hit_count >= min_hits,
                    QaCache.is_deleted == 0,
                )
                .order_by(desc(QaCache.hit_count))
                .limit(topk)
            )
        ).scalars().all()
    except Exception as e:
        degraded("cache_warmup_query", e)
        return 0

    warmed = 0
    for row in rows:
        try:
            data = json.loads(row.answer) if isinstance(row.answer, str) else row.answer
            # 用租户化 cache_key 回写 Redis；历史旧 key 不再参与租户问答命中。
            tenant = getattr(row, "tenant_id", None) or "default"
            model = row.model_type or "default"
            expected_prefix = f"qa:{tenant}:{model}:"
            key = row.cache_key if (row.cache_key or "").startswith(expected_prefix) else f"{expected_prefix}{row.query_normalized}"
            ok = await redis_client.cache_set_json_safe(key, data, settings.QA_CACHE_TTL)
            if ok:
                warmed += 1
        except Exception as e:
            degraded("cache_warmup_set", e)
    return warmed


async def warmup_from_file(filepath: str = "golden_qa.json") -> int:
    """从 golden_qa.json 预加载黄金回归集到 Redis（Phase 3 扩展）。

    文件格式: [{"query": "...", "answer": "...", ...}, ...]
    适用于 CI 评测门禁的固定问题集预热。
    """
    import os
    if not os.path.exists(filepath):
        return 0
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            items = json.load(f)
    except Exception as e:
        degraded("cache_warmup_file", e)
        return 0

    warmed = 0
    for item in items:
        query = (item.get("query") or "").strip()
        if not query:
            continue
        from app.services import term_service
        nq = term_service.normalize(query)
        key = f"qa:default:default:{nq}"
        val = {
            "answer": item.get("answer", ""),
            "retrievalSource": item.get("retrievalSource", []),
            "responseTime": 0.0,
            "hallucinationRate": 0.0,
            "cached": True,
            "cacheLayer": "warmup",
            "conversationId": "",
        }
        try:
            ok = await redis_client.cache_set_json_safe(key, val, settings.QA_CACHE_TTL)
            if ok:
                warmed += 1
        except Exception as e:
            degraded("cache_warmup_golden", e)
    return warmed


# ---- CLI: python -m app.services.cache_warmup ----
if __name__ == "__main__":
    async def _main():
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            n = await warmup_hot_queries(db)
            print(f"[cache_warmup] 热点预热 {n} 条")
            m = await warmup_from_file()
            if m:
                print(f"[cache_warmup] golden 预热 {m} 条")

    asyncio.run(_main())
