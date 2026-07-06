"""rewrite-stats / rewrite-events 接口测试（httpx 连容器内 localhost:8001）。"""
import asyncio

import httpx


def test_rewrite_stats_period_7d():
    """period=7d 参数生效（DEBUG 模式认证宽松，无 token 也 200，故测参数而非 401）。"""
    async def go():
        async with httpx.AsyncClient(base_url="http://localhost:8001") as c:
            r = await c.get("/api/system/optimizer/rewrite-stats?period=7d")
            assert r.status_code == 200
            assert "total" in r.json()["data"]
    asyncio.run(go())


def test_rewrite_stats_ok():
    """admin token → 200 + 含 total 字段。"""
    async def go():
        async with httpx.AsyncClient(base_url="http://localhost:8001") as c:
            tok = (await c.post("/api/system/login",
                                json={"username": "admin", "password": "admin123"})).json()["data"]["token"]
            r = await c.get("/api/system/optimizer/rewrite-stats",
                            headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200
            assert "total" in r.json()["data"]
    asyncio.run(go())


def test_rewrite_events_ok():
    """admin token → 200 + 含 list 字段。"""
    async def go():
        async with httpx.AsyncClient(base_url="http://localhost:8001") as c:
            tok = (await c.post("/api/system/login",
                                json={"username": "admin", "password": "admin123"})).json()["data"]["token"]
            r = await c.get("/api/system/optimizer/rewrite-events?strategy=rewrite",
                            headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200
            assert "list" in r.json()["data"]
    asyncio.run(go())
