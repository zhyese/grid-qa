"""安全启动自检单测：占位密钥/默认口令/DEBUG 三分支 + 指标置数 + 干净路径。"""

from app.config import settings
from app.core import security_check as sc


def test_all_insecure_branches_hit(monkeypatch, caplog):
    monkeypatch.setattr(settings, "JWT_SECRET", "please-change-this-to-a-random-long-string")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "admin123")
    monkeypatch.setattr(settings, "DEBUG", True)
    items = sc.insecure_settings()
    codes = {x["code"] for x in items}
    assert codes == {"jwt_secret_placeholder", "admin_password_default", "debug_on"}
    with caplog.at_level("WARNING", logger="app"):
        n = sc.run_startup_check()
    assert n == 3
    assert sum(1 for r in caplog.records if "不安全配置" in r.message) == 3


def test_clean_config_passes(monkeypatch, caplog):
    monkeypatch.setattr(settings, "JWT_SECRET", "x" * 64)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "n0t-default!")
    monkeypatch.setattr(settings, "DEBUG", False)
    assert sc.insecure_settings() == []
    with caplog.at_level("INFO", logger="app"):
        assert sc.run_startup_check() == 0
    assert any("自检通过" in r.message for r in caplog.records)


def test_metric_set(monkeypatch):
    from app.core.metrics import INSECURE_CONFIG

    monkeypatch.setattr(settings, "JWT_SECRET", "please-change-this-to-a-random-long-string")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "strong-pass")
    monkeypatch.setattr(settings, "DEBUG", False)
    sc.run_startup_check()
    assert INSECURE_CONFIG._value.get() == 1.0


def test_no_secret_value_in_output(monkeypatch):
    """输出只含 code/detail，绝不回传配置真实值（防日志泄密）。"""
    monkeypatch.setattr(settings, "JWT_SECRET", "please-change-this-to-a-random-long-string")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "admin123")
    monkeypatch.setattr(settings, "DEBUG", False)
    blob = repr(sc.insecure_settings())
    assert "admin123" not in blob and "please-change" not in blob
