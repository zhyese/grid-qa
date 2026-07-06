"""query_rewrite_v2 集成测试：Classifier+Cache+Evaluator 协同。mock LLM/cache/eval。"""
import asyncio
from unittest.mock import AsyncMock, patch

from app.services import query_rewrite


def test_normal_query_skipped():
    """规范 query 被 Classifier 判 normal → 直接返回原 query，不调 LLM。"""
    async def go():
        with patch.object(query_rewrite, "get_llm_provider") as mk_llm:
            r = await query_rewrite.rewrite_query_v2("主变压器绕组温度过热的应急处置步骤", None)
            assert r["query"] == "主变压器绕组温度过热的应急处置步骤"
            assert r["strategy"] == "normal"
            assert r["cached"] is False
            mk_llm.assert_not_called()
    asyncio.run(go())


def test_cache_hit_skips_llm():
    """缓存命中 → 用缓存结果，不调 LLM。"""
    async def go():
        with patch.object(query_rewrite.rewrite_cache, "get",
                          AsyncMock(return_value={"result": "改写后", "improved": True})):
            with patch.object(query_rewrite, "get_llm_provider") as mk_llm:
                r = await query_rewrite.rewrite_query_v2("咋办", None)
                assert r["cached"] is True
                assert r["query"] == "改写后"
                assert r["improved"] is True
                mk_llm.assert_not_called()
    asyncio.run(go())


def test_colloquial_goes_through_llm_and_eval():
    """口语 query → 调 LLM 改写 + 评估（mock 评估否决 → 用原 query）。"""
    async def go():
        with patch.object(query_rewrite.rewrite_cache, "get", AsyncMock(return_value=None)):
            with patch.object(query_rewrite.rewrite_cache, "set", AsyncMock(return_value=True)):
                with patch.object(query_rewrite, "get_llm_provider") as mk_llm:
                    mk_llm.return_value.chat = AsyncMock(return_value="主变压器故障应急处置")
                    with patch.object(query_rewrite.rewrite_evaluator, "evaluate",
                                      AsyncMock(return_value={"improved": False, "orig_score": 0.5, "new_score": 0.4})):
                        r = await query_rewrite.rewrite_query_v2("主变烧了咋办", None)
                assert r["strategy"] == "colloquial"
                assert mk_llm.return_value.chat.called
                assert r["improved"] is False
                assert r["query"] == "主变烧了咋办"  # 评估否决 → 用原 query
    asyncio.run(go())
