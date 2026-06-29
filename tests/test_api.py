"""API 冒烟集成测试（需后端运行在 8001，未运行则跳过）。"""
import pytest

pytestmark = pytest.mark.integration


def test_health():
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:8001/health", timeout=5)
    except Exception:
        pytest.skip("后端未运行，跳过")
    assert r.status_code == 200
    assert r.json()["data"]["status"] in ("healthy", "degraded")


def test_login():
    try:
        import httpx
        r = httpx.post(
            "http://127.0.0.1:8001/api/system/login",
            json={"username": "admin", "password": "admin123"},
            timeout=5,
        )
    except Exception:
        pytest.skip("后端未运行，跳过")
    assert r.status_code == 200
    assert r.json()["code"] == 200
