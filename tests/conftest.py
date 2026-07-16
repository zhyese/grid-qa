"""pytest 配置：把 backend 加入 sys.path。"""
import sys
import asyncio
import pytest
import pytest_asyncio
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


@pytest_asyncio.fixture
async def test_db():
    """sqlite in-memory 单测会话（unit test 用；集成测试连真实 MySQL 不用它）。

    容器内已装 aiosqlite；本地 venv 跑需 `pip install aiosqlite`。
    """
    from sqlalchemy.ext.asyncio import (
        create_async_engine as _cae, async_sessionmaker as _asm, AsyncSession as _AS,
    )
    from app.db.base import Base
    import app.db.init_db  # noqa: F401  触发全部模型注册到 Base.metadata（init_db 顶部 import 了所有 model，含 Document 等 FK 依赖）
    _eng = _cae("sqlite+aiosqlite:///:memory:")
    async with _eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _S = _asm(_eng, class_=_AS, expire_on_commit=False)
    async with _S() as s:
        yield s
    await _eng.dispose()
