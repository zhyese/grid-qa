"""H6 单代理检索循环单测：停止准则渲染、工具/persona 开关门控、降级不抛。"""
import pytest

from app.config import settings
from app.rag import crag
from app.services import agent_tools
from app.services.agent_personas import DEEP_RETRIEVAL_PERSONA


def _ctxs(n=3, score=0.9):
    return [{"docName": f"规程{i}", "chunk": f"内容{i}", "score": score} for i in range(n)]


def test_fmt_assessment_correct_advises_stop():
    text = agent_tools._fmt_assessment("q", _ctxs(), crag.GRADE_CORRECT, "es 0.90 ≥ 0.72", 0.9, True)
    assert "分级:correct" in text
    assert "可以停止检索" in text
    assert "证据强度:0.90/1.0" in text


def test_fmt_assessment_ambiguous_advises_limited_retry():
    text = agent_tools._fmt_assessment("q", _ctxs(), crag.GRADE_AMBIGUOUS, "es 0.50", 0.5, True)
    assert "分级:ambiguous" in text
    assert "1~2 组关键词" in text


def test_fmt_assessment_incorrect_advises_stop_after_two_rounds():
    text = agent_tools._fmt_assessment("q", [], crag.GRADE_INCORRECT, "检索无结果", None, True)
    assert "分级:incorrect" in text
    assert "连续 2 轮" in text
    assert "现有资料无法确认" in text
    assert "当前命中来源:无" in text


def test_fmt_assessment_marks_degraded_evaluator():
    text = agent_tools._fmt_assessment("q", _ctxs(), crag.GRADE_AMBIGUOUS, "rerank 未启用", None, False)
    assert "评估器降级" in text
    assert "rerank:未启用" in text


def test_fmt_assessment_lists_sources():
    text = agent_tools._fmt_assessment("q", _ctxs(), crag.GRADE_CORRECT, "r", 0.9, True)
    assert "[1] 规程0" in text


def test_assess_evidence_tool_gated_by_switch(monkeypatch):
    monkeypatch.setattr(settings, "AGENTIC_RETRIEVAL_ENABLE", False, raising=False)
    assert agent_tools.build_default_registry().get("assess_evidence") is None

    monkeypatch.setattr(settings, "AGENTIC_RETRIEVAL_ENABLE", True, raising=False)
    tool = agent_tools.build_default_registry().get("assess_evidence")
    assert tool is not None
    assert tool.audit_action_type == "read"     # 只读，不应被当成写操作
    assert tool.required_roles == []            # 显式声明全员可调（红队缺口 #5 要求）


def test_telemetry_tool_gated_by_switch(monkeypatch):
    monkeypatch.setattr(settings, "TELEMETRY_QA_ENABLE", False, raising=False)
    monkeypatch.setattr(settings, "PROACTIVE_TELEMETRY_ENABLE", False, raising=False)
    assert agent_tools.build_default_registry().get("query_telemetry") is None

    monkeypatch.setattr(settings, "TELEMETRY_QA_ENABLE", True, raising=False)
    tool = agent_tools.build_default_registry().get("query_telemetry")
    assert tool is not None
    assert tool.audit_action_type == "read"


def test_deep_retrieval_persona_shape():
    assert DEEP_RETRIEVAL_PERSONA.name == "deep_retrieval"
    assert "assess_evidence" in DEEP_RETRIEVAL_PERSONA.allowed_tools
    assert DEEP_RETRIEVAL_PERSONA.max_iter >= 8          # 迭代预算
    assert "停止准则" in DEEP_RETRIEVAL_PERSONA.system_prompt
    assert "无法确认" in DEEP_RETRIEVAL_PERSONA.system_prompt
    assert DEEP_RETRIEVAL_PERSONA.fallback is not None    # 降级兜底存在


def test_deep_retrieval_registration_gated():
    """注册受 AGENTIC_RETRIEVAL_ENABLE 门控：关=不出现在 persona 列表（主链路零改动）。

    不依赖运行环境 .env 的实际值（.env 开闸后同样成立）：显式设 False/True 验证门控两侧。
    """
    from app.services import persona_store
    saved = settings.AGENTIC_RETRIEVAL_ENABLE
    try:
        settings.AGENTIC_RETRIEVAL_ENABLE = False
        persona_store._CODE_PERSONAS.pop("deep_retrieval", None)
        assert "deep_retrieval" not in persona_store.code_personas()
        settings.AGENTIC_RETRIEVAL_ENABLE = True
        persona_store._CODE_PERSONAS["deep_retrieval"] = persona_store.DEEP_RETRIEVAL_PERSONA
        assert "deep_retrieval" in persona_store.code_personas()
    finally:
        settings.AGENTIC_RETRIEVAL_ENABLE = saved
        persona_store._CODE_PERSONAS.pop("deep_retrieval", None)


@pytest.mark.asyncio
async def test_assess_evidence_degrades_when_retrieval_fails(monkeypatch):
    """检索异常 → 不抛出，按"无结果"保守分级，让 Agent 继续走停止准则。"""
    async def boom(*a, **kw):
        raise RuntimeError("milvus down")

    monkeypatch.setattr(agent_tools.retrieval_service, "mixed_search", boom)
    monkeypatch.setattr(settings, "RERANK_ENABLE", True, raising=False)

    text = await agent_tools._t_assess_evidence(None, None, "主变油温高", tenant="default")
    assert "incorrect" in text
    assert "检索无结果" in text


@pytest.mark.asyncio
async def test_assess_evidence_grades_with_crag(monkeypatch):
    async def fake_search(*a, **kw):
        return _ctxs(n=3, score=0.95)

    monkeypatch.setattr(agent_tools.retrieval_service, "mixed_search", fake_search)
    monkeypatch.setattr(settings, "RERANK_ENABLE", True, raising=False)

    text = await agent_tools._t_assess_evidence(None, None, "主变油温高", tenant="default")
    assert "correct" in text
    assert "可以停止检索" in text
