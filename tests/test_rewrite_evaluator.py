"""RewriteEvaluator 单测：分数对比 + margin + 异常回退。mock _light_dense 避免真检索。"""
import asyncio
from unittest.mock import AsyncMock, patch

from app.services import rewrite_evaluator


def test_improved_when_new_higher():
    """new 分数和 > orig*(1+margin) → improved。"""
    async def go():
        def fake(q, mt):
            return [{"score": 0.2}] * 5 if q == "orig" else [{"score": 0.3}] * 5
        with patch.object(rewrite_evaluator, "_light_dense", AsyncMock(side_effect=fake)):
            r = await rewrite_evaluator.evaluate("orig", "rewritten", None)
        assert r["improved"] is True
        assert r["orig_score"] < r["new_score"]
    asyncio.run(go())


def test_reject_when_not_better():
    """分数接近（< margin）→ not improved。"""
    async def go():
        same = [{"score": 0.2}] * 5
        with patch.object(rewrite_evaluator, "_light_dense", AsyncMock(return_value=same)):
            r = await rewrite_evaluator.evaluate("orig", "rewritten", None)
        assert r["improved"] is False
    asyncio.run(go())


def test_exception_returns_not_improved():
    """检索异常 → 回退 not improved（不抛）。"""
    async def go():
        with patch.object(rewrite_evaluator, "_light_dense", AsyncMock(side_effect=RuntimeError("boom"))):
            r = await rewrite_evaluator.evaluate("orig", "rewritten", None)
        assert r["improved"] is False
    asyncio.run(go())
