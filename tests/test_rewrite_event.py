"""RewriteEvent service 单测：log 写入 + stats 聚合 + events_page 分页（用容器真 DB）。"""
import asyncio
import pytest

from app.services import rewrite_event_service as svc

pytestmark = pytest.mark.integration  # 依赖容器真 DB（MySQL），CI 无 DB 跳过


def test_log_and_stats():
    """log 写一条 → stats 能读到，结构完整。"""
    async def go():
        await svc.log("rewrite", "test_q_rewrite_event", "test_qr", True,
                      0.1, 0.2, cached=False, route="hybrid")
        s = await svc.stats("today")
        assert s["total"] >= 1
        assert 0.0 <= s["adoptedRate"] <= 1.0
        assert "byStrategy" in s
    asyncio.run(go())


def test_events_page_structure():
    """events_page 返回 {total, list} 结构，list 元素含必要字段。"""
    async def go():
        r = await svc.events_page(page=1, size=5, strategy="rewrite")
        assert "total" in r and "list" in r
        if r["list"]:
            e = r["list"][0]
            assert {"ts", "strategy", "original", "rewritten", "improved", "origScore", "newScore"} <= set(e)
    asyncio.run(go())
