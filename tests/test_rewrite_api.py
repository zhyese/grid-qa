"""rewrite-stats / rewrite-events 接口测试（httpx 连容器内 localhost:8001）。"""
import asyncio

import httpx


def test_rewrite_stats_requires_admin():
    """无 token → 401。"""
    async def go():
        async with httpx.AsyncClient(base_url="http://localhost:8001") as c:
            r = await c.get("/api/system/optimizer/rewrite-stats")
            assert r.status_code == 401
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
