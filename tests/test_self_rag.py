"""Self-RAG 检索必要性判断单测（关闭态纯逻辑）。"""
import asyncio

from app.services import self_rag


def test_need_retrieve_disabled_returns_true():
    """SELF_RAG_ENABLE 默认关 → 保守返回 True（需检索，不影响主流程）。"""
    assert asyncio.run(self_rag.need_retrieve("今天天气怎么样")) is True


def test_need_retrieve_empty_returns_true():
    assert asyncio.run(self_rag.need_retrieve("")) is True


def test_skip_answer_covers_domain():
    """拒答文案明确说明仅服务电网运维范畴。"""
    assert "不属于" in self_rag.SKIP_ANSWER
    assert "运维" in self_rag.SKIP_ANSWER
