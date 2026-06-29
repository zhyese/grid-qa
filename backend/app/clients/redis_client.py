"""Redis 异步客户端（热点问答缓存）。"""
import json
from typing import Optional

import redis.asyncio as aioredis

from app.config import settings

_pool: Optional[aioredis.Redis] = None


def get_redis() -> aioredis.Redis:
    global _pool
    if _pool is None:
        _pool = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _pool


async def cache_get_json(key: str):
    v = await get_redis().get(key)
    return json.loads(v) if v else None


async def cache_set_json(key: str, value: dict, ttl: int) -> None:
    await get_redis().set(key, json.dumps(value, ensure_ascii=False), ex=ttl)


async def cache_set_json_persistent(key: str, value: dict) -> None:
    """无 TTL 永久存储（配置持久化）。"""
    await get_redis().set(key, json.dumps(value, ensure_ascii=False))


async def ping() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False
