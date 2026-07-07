"""evidence_gap_service 单测：collect 去重 + list/get 结构（容器真 DB）。"""
import asyncio
from app.services import evidence_gap_service as svc


def test_collect_dedup():
    """同 query pending 去重：第二次 collect 返回 0。用唯一 query 避开历史残留。"""
    async def go():
        q = "测试_dedup_unique_20260707_xyz"
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
        q = "测试_get_unique_20260707_abc"
        gid = await svc.collect(q, "答", "refused", "incorrect", "refused")
        g = await svc.get_gap(gid)
        assert g and g["query"] == q
    asyncio.run(go())
