"""rewrite-stats / rewrite-events 接口测试（httpx 连容器内 localhost:8001）。"""
import asyncio
import httpx
import pytest

def _is_backend_down():
    try:
        r = httpx.get("http://localhost:8001/health", timeout=2)
        return r.status_code != 200
    except Exception:
        return True

if _is_backend_down():
    pytestmark = pytest.mark.skip("后端未运行，跳过该 API 测试")


def test_rewrite_stats_requires_admin():
    """无 token → 业务 code 401（项目统一封装：HTTP 恒 200，认证失败体现在 body code）。"""
    async def go():
        async with httpx.AsyncClient(base_url="http://localhost:8001") as c:
            r = await c.get("/api/system/optimizer/rewrite-stats")
            assert r.status_code == 200  # HTTP 恒 200
            assert r.json()["code"] == 401  # 业务码 401 = 未登录
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
