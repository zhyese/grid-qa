"""RewriteCache 单测：Redis 读写 + miss + 损坏降级。asyncio.run 包装不依赖 pytest-asyncio。"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

from app.services import rewrite_cache


def test_set_then_get():
    async def go():
        with patch.object(rewrite_cache.redis_client, "get_redis") as mk:
            r = AsyncMock()
            r.set = AsyncMock(return_value=True)
            r.get = AsyncMock(return_value=json.dumps({"result": "改写后", "improved": True}))
            mk.return_value = r
            ok = await rewrite_cache.set("rewrite", "q", {"result": "改写后", "improved": True})
            assert ok is True
            got = await rewrite_cache.get("rewrite", "q")
            assert got == {"result": "改写后", "improved": True}
    asyncio.run(go())


def test_get_miss_returns_none():
    async def go():
        with patch.object(rewrite_cache.redis_client, "get_redis") as mk:
            r = AsyncMock()
            r.get = AsyncMock(return_value=None)
            mk.return_value = r
            assert await rewrite_cache.get("rewrite", "q") is None
    asyncio.run(go())


def test_get_corrupt_returns_none():
    async def go():
        with patch.object(rewrite_cache.redis_client, "get_redis") as mk:
            r = AsyncMock()
            r.get = AsyncMock(return_value="not json")
            mk.return_value = r
            assert await rewrite_cache.get("rewrite", "q") is None
    asyncio.run(go())


def test_get_exception_returns_none():
    """Redis 异常 → 降级返回 None（不抛）。"""
    async def go():
        with patch.object(rewrite_cache.redis_client, "get_redis", side_effect=RuntimeError("redis down")):
            assert await rewrite_cache.get("rewrite", "q") is None
    asyncio.run(go())
