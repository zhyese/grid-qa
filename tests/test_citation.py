"""证据溯源 auto_cite + 配置项单测。"""
from app.config import settings


def test_citation_settings_defaults():
    assert settings.CITATION_AUTO_ENABLE is True
    assert settings.CITATION_SIM_THRESHOLD == 0.6
