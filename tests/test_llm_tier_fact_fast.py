"""FACT_FAST 分档单测：开关默认关=现状（事实短问走 plus）；开=走 turbo。"""

from app.config import settings
from app.providers.llm_router import classify_llm

# 2026-09-17 实测样本：原判"默认质量档"(plus)，turbo 7.4s vs plus 15.2s 同 grade
FACT_QUERY = "变压器日常巡视检查哪些项目？"


def test_fact_fast_off_is_status_quo(monkeypatch):
    monkeypatch.setattr(settings, "LLM_TIER_ENABLE", True)
    monkeypatch.setattr(settings, "LLM_TIER_FACT_FAST_ENABLE", False)
    tier, reason = classify_llm(FACT_QUERY)
    assert tier == "plus"
    assert reason == "默认质量档"


def test_fact_fast_on_routes_turbo(monkeypatch):
    monkeypatch.setattr(settings, "LLM_TIER_ENABLE", True)
    monkeypatch.setattr(settings, "LLM_TIER_FACT_FAST_ENABLE", True)
    tier, reason = classify_llm(FACT_QUERY)
    assert tier == "turbo"
    assert "事实短问" in reason


def test_fault_query_stays_plus_even_when_fast_on(monkeypatch):
    """故障诊断必须留 plus——FACT_FAST 不许吞掉推理档。"""
    monkeypatch.setattr(settings, "LLM_TIER_ENABLE", True)
    monkeypatch.setattr(settings, "LLM_TIER_FACT_FAST_ENABLE", True)
    tier, _ = classify_llm("变压器油温异常升高还伴有异响是什么故障？")
    assert tier == "plus"


def test_numeric_param_stays_plus(monkeypatch):
    monkeypatch.setattr(settings, "LLM_TIER_ENABLE", True)
    monkeypatch.setattr(settings, "LLM_TIER_FACT_FAST_ENABLE", True)
    tier, _ = classify_llm("110kV 变压器日常巡视检查哪些项目？")
    assert tier == "plus"


def test_default_off_in_config():
    """仓库铁律：新开关默认 False = 现状行为。"""
    assert settings.LLM_TIER_FACT_FAST_ENABLE is False


def test_no_domain_term_stays_plus(monkeypatch):
    """无领域词的 natural 闲聊不许进 turbo 快道（密度=0）。"""
    monkeypatch.setattr(settings, "LLM_TIER_ENABLE", True)
    monkeypatch.setattr(settings, "LLM_TIER_FACT_FAST_ENABLE", True)
    tier, _ = classify_llm("你能帮我做点什么呢")
    assert tier == "plus"
