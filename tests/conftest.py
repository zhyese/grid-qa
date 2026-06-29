"""pytest 配置：把 backend 加入 sys.path。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: 需要后端服务运行的集成测试")
