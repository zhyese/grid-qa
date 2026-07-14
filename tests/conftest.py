"""pytest 配置：把 backend 加入 sys.path。"""
import sys
import asyncio
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.db.session import engine


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: 需要后端服务运行的集成测试")


@pytest.fixture(autouse=True)
def cleanup_database_pool():
    yield
    try:
        async def dispose():
            await engine.dispose()
        asyncio.run(dispose())
    except Exception:
        pass
