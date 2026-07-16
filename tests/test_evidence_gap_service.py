"""evidence_gap_service 单测：collect 去重 + list/get 结构（容器真 DB）。"""
import asyncio
from datetime import datetime
import pytest
from app.services import evidence_gap_service as svc

pytestmark = pytest.mark.integration  # 依赖容器真 DB（MySQL），CI 无 DB 跳过


def test_collect_dedup():
    """同 query pending 去重：第二次 collect 返回 0。用时间戳唯一 query 避开历史残留。"""
    async def go():
        q = f"测试_dedup_{datetime.now().isoformat()}"
        id1 = await svc.collect(q, "答案", "medium", "ambiguous", "normal")
        assert id1 > 0
        id2 = await svc.collect(q, "答案", "medium", "ambiguous", "normal")
        assert id2 == 0  # 去重
    asyncio.run(go())


def test_list_gaps_structure():
    async def go():
        r = await svc.list_gaps("pending", 1, 20)
        assert "list" in r and "total" in r
    asyncio.run(go())


def test_get_gap():
    async def go():
        q = f"测试_get_{datetime.now().isoformat()}"
        gid = await svc.collect(q, "答", "refused", "incorrect", "refused")
        g = await svc.get_gap(gid)
        assert g and g["query"] == q
    asyncio.run(go())
