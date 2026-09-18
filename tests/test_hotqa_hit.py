"""hotqa 复用契约测试：点赞写入的高频问答对在 qa_service 查询时精确命中（永久缓存）。

覆盖三条契约：
1. hotqa:{nq} 有值 → _hit_hotqa 返回完整 answer dict（cacheLayer="hotqa"）
2. hotqa 无值 → _hit_hotqa 返回 None（走正常检索/缓存链路）
3. HOTQA_ENABLE=False → 不查 hotqa（opt-out）
4. Redis 异常 → 降级吞掉返回 None（不影响主链路）

不依赖后端服务（FakeRedis + monkeypatch settings）；asyncio.run 包装不依赖 pytest-asyncio。
唯一 nq 隔离；带 integration mark（按仓库惯例标注，便于 CI 分类）。
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services import qa_service


pytestmark = pytest.mark.integration


_HOTQA_NQ = "变压器套管发热缺陷处置unique_hotqa_test_2026"


def _fake_redis_with_hotqa(payload: dict | None):
    """构造一个 fake redis，hotqa:{nq} 命中 payload（None 表示 miss）。"""
    r = AsyncMock()
    if payload is None:
        r.get = AsyncMock(return_value=None)
    else:
        r.get = AsyncMock(return_value=json.dumps(payload, ensure_ascii=False))
    # sismember 默认返回 False（不在黑名单），防止 AsyncMock 自动创建返回 truthy 值
    r.sismember = AsyncMock(return_value=False)
    return r


def test_hit_hotqa_returns_dict_when_key_present():
    """hotqa:{nq} 有值 → 返回完整 answer dict，cacheLayer=hotqa，confidence=high。"""
    payload = {
        "query": _HOTQA_NQ,
        "answer": "套管发热应立即降压运行并安排检修。",
        "sources": "规程A.docx,规程B.docx",
        "count": 3,
        "lastLikedAt": 1780000000,
        "tenant": "default",
    }

    async def go():
        with patch.object(qa_service.redis_client, "get_redis") as mk:
            mk.return_value = _fake_redis_with_hotqa(payload)
            hot = await qa_service._hit_hotqa(_HOTQA_NQ, "conv-xyz", 1000.0)
        assert hot is not None
        assert hot["answer"] == payload["answer"]
        assert hot["cacheLayer"] == "hotqa"
        assert hot["cached"] is True
        assert hot["confidence"] == "high"
        assert hot["conversationId"] == "conv-xyz"
        assert hot["hotqaCount"] == 3
        # sources 逗号串 → retrievalSource 列表（docName 占位）
        names = [s["docName"] for s in hot["retrievalSource"]]
        assert names == ["规程A.docx", "规程B.docx"]
        # 占位字段
        assert hot["retrievalSource"][0]["docId"] == ""
        assert hot["retrievalSource"][0]["score"] == 0.0

    asyncio.run(go())


def test_hit_hotqa_returns_none_when_key_missing():
    """hotqa:{nq} 无值 → 返回 None（走正常检索/缓存）。"""
    async def go():
        with patch.object(qa_service.redis_client, "get_redis") as mk:
            mk.return_value = _fake_redis_with_hotqa(None)
            assert await qa_service._hit_hotqa(_HOTQA_NQ, "", 0.0) is None
    asyncio.run(go())


def test_hit_hotqa_disabled_when_flag_off():
    """HOTQA_ENABLE=False → 不查 Redis，直接返回 None。"""
    async def go():
        # 即便 Redis 里有值，开关关掉也应返回 None
        with patch.object(qa_service.settings, "HOTQA_ENABLE", False):
            with patch.object(qa_service.redis_client, "get_redis") as mk:
                mk.return_value = _fake_redis_with_hotqa({"answer": "X"})
                assert await qa_service._hit_hotqa(_HOTQA_NQ, "", 0.0) is None
        # 且不应触碰 Redis（get 调用次数 == 0）
        with patch.object(qa_service.settings, "HOTQA_ENABLE", False):
            with patch.object(qa_service.redis_client, "get_redis") as mk:
                fake = _fake_redis_with_hotqa({"answer": "X"})
                mk.return_value = fake
                await qa_service._hit_hotqa(_HOTQA_NQ, "", 0.0)
                assert fake.get.await_count == 0
    asyncio.run(go())


def test_hit_hotqa_swallows_redis_exception():
    """Redis 异常 → degraded 吞掉返回 None（不抛，不影响主链路）。"""
    async def go():
        with patch.object(qa_service.redis_client, "get_redis", side_effect=RuntimeError("redis down")):
            assert await qa_service._hit_hotqa(_HOTQA_NQ, "", 0.0) is None
    asyncio.run(go())


def test_hit_hotqa_swallows_corrupt_json():
    """hotqa 值损坏（非 JSON）→ 降级返回 None。"""
    async def go():
        with patch.object(qa_service.redis_client, "get_redis") as mk:
            fake = AsyncMock()
            fake.get = AsyncMock(return_value="not json {{{")
            mk.return_value = fake
            assert await qa_service._hit_hotqa(_HOTQA_NQ, "", 0.0) is None
    asyncio.run(go())


def test_hit_hotqa_empty_nq_returns_none():
    """空 nq → 直接返回 None（不查 Redis）。"""
    async def go():
        with patch.object(qa_service.redis_client, "get_redis") as mk:
            fake = AsyncMock()
            fake.get = AsyncMock(return_value=json.dumps({"answer": "x"}))
            mk.return_value = fake
            assert await qa_service._hit_hotqa("", "", 0.0) is None
            assert fake.get.await_count == 0
    asyncio.run(go())


def test_hit_hotqa_empty_sources_yields_empty_list():
    """sources 为空字符串 → retrievalSource=[]（不构造空占位项）。"""
    async def go():
        with patch.object(qa_service.redis_client, "get_redis") as mk:
            mk.return_value = _fake_redis_with_hotqa({
                "answer": "ans", "sources": "",
            })
            hot = await qa_service._hit_hotqa(_HOTQA_NQ, "", 0.0)
            assert hot is not None
            assert hot["retrievalSource"] == []
    asyncio.run(go())


def test_hit_hotqa_blacklisted_returns_none():
    """query 在黑名单中 → hotqa 不命中（黑名单优先，该答案被多人 dislike 过）。"""
    payload = {
        "query": _HOTQA_NQ,
        "answer": "已被踩过的答案",
        "sources": "doc.docx",
        "count": 5,
        "tenant": "default",
    }

    async def go():
        with patch.object(qa_service.redis_client, "get_redis") as mk:
            mk.return_value = _fake_redis_with_hotqa(payload)
            with patch.object(qa_service, "_is_blacklisted", return_value=True):
                hot = await qa_service._hit_hotqa(_HOTQA_NQ, "", 0.0)
        assert hot is None
    asyncio.run(go())
