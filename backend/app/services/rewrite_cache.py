"""改写结果 Redis 缓存：相同 query+strategy 不重复调 LLM。

key: rewrite:{strategy}:{md5(query)}，TTL 由 REWRITE_CACHE_TTL 控制（默认 7 天）。
异常统一降级（返回 None / False），不阻塞改写主流程。
"""
import hashlib
import json

from app.clients import redis_client
from app.config import settings
from app.core.obs import degraded


def _key(strategy: str, query: str) -> str:
    h = hashlib.md5(query.encode("utf-8")).hexdigest()
    return f"rewrite:{strategy}:{h}"


async def get(strategy: str, query: str) -> dict | None:
    """读缓存。miss/损坏/异常 → None（调用方走 LLM）。"""
    try:
        v = await redis_client.get_redis().get(_key(strategy, query))
        return json.loads(v) if v else None
    except Exception as e:
        degraded("rewrite_cache_get", e)
        return None


async def set(strategy: str, query: str, value: dict) -> bool:
    """写缓存。失败降级返回 False（不阻塞）。"""
    try:
        await redis_client.get_redis().set(
            _key(strategy, query),
            json.dumps(value, ensure_ascii=False),
            ex=settings.REWRITE_CACHE_TTL,
        )
        return True
    except Exception as e:
        degraded("rewrite_cache_set", e)
        return False
