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
    """L1 读缓存。B2：命中时可选滑动续期（CACHE_SLIDE_TTL_ENABLE，默认关）。

    开关关 → 现状零破坏（命中不 EXPIRE）；开关开 → 命中时 EXPIRE key QA_CACHE_TTL
    让热 query 保活，防 LRU evict。EXPIRE 异常静默吞，不影响读。
    """
    r = get_redis()
    v = await r.get(key)
    if not v:
        return None
    if getattr(settings, "CACHE_SLIDE_TTL_ENABLE", False):
        try:
            await r.expire(key, settings.QA_CACHE_TTL)
        except Exception:
            pass  # EXPIRE 失败不阻塞读（键值已拿到）
    return json.loads(v)


async def cache_set_json(key: str, value: dict, ttl: int) -> None:
    await get_redis().set(key, json.dumps(value, ensure_ascii=False), ex=ttl)


async def cache_set_json_persistent(key: str, value: dict) -> None:
    """无 TTL 永久存储（配置持久化）。"""
    await get_redis().set(key, json.dumps(value, ensure_ascii=False))


async def cache_get_json_safe(key: str) -> dict | None:
    """安全读缓存：异常时返回 None（不抛，调用方走降级路径）。"""
    try:
        v = await get_redis().get(key)
        return json.loads(v) if v else None
    except Exception:
        return None


async def cache_set_json_safe(key: str, value: dict, ttl: int) -> bool:
    """安全写缓存：异常时返回 False（调用方记录降级，不阻塞业务）。"""
    try:
        await get_redis().set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
        return True
    except Exception:
        return False


async def ping() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False
