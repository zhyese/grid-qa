"""mixed_search overrides 参数单测（保护 13 caller 默认路径）。

只测 _ov 纯函数 + mixed_search 签名（不依赖 db/Milvus，稳定）；
mixed_search 实际检索行为由集成测试覆盖。
"""
import inspect


def test_ov_returns_override_when_present():
    from app.services.retrieval_service import _ov
    assert _ov({"RRF_K": 40}, "RRF_K", 60) == 40


def test_ov_returns_default_when_none():
    from app.services.retrieval_service import _ov
    assert _ov(None, "RRF_K", 60) == 60


def test_ov_returns_default_when_missing():
    from app.services.retrieval_service import _ov
    assert _ov({"OTHER": 1}, "RRF_K", 60) == 60


def test_ov_returns_default_when_empty():
    from app.services.retrieval_service import _ov
    assert _ov({}, "RRF_K", 60) == 60


def test_mixed_search_accepts_overrides_param():
    """mixed_search 签名接受 overrides 可选参数（None 默认，13 caller 零破坏）。"""
    from app.services.retrieval_service import mixed_search
    sig = inspect.signature(mixed_search)
    assert "overrides" in sig.parameters
    assert sig.parameters["overrides"].default is None


def test_tune_config_defaults():
    from app.config import Settings
    s = Settings()
    assert s.TUNE_ENABLE is True
    assert s.TUNE_MIN_IMPROVE == 0.02
    assert s.TUNE_MIN_SAMPLE == 10
    assert s.TUNE_SCAN_TOPK == 5
