"""F1: list_gaps source 过滤（auto_crag/auto_no_recall/manual 等来源筛选）。"""
import pytest

pytestmark = pytest.mark.integration  # 依赖容器真 DB（MySQL），CI 无 DB 跳过


@pytest.mark.asyncio
async def test_list_gaps_filter_by_source():
    """list_gaps(source='auto_crag') 只返回该来源（用唯一 query 避开历史残留）。"""
    from datetime import datetime
    from app.services import evidence_gap_service as eg
    from app.db.session import AsyncSessionLocal
    from app.models.evidence_gap import EvidenceGap

    suffix = datetime.now().isoformat().replace(":", "").replace(".", "")
    q_crag = f"filter_src_crag_{suffix}"
    q_manual = f"filter_src_manual_{suffix}"

    async with AsyncSessionLocal() as db:
        db.add(EvidenceGap(query=q_crag, status="pending", confidence="refused",
                           source="auto_crag", original_answer="", grade="", crag_action=""))
        db.add(EvidenceGap(query=q_manual, status="pending", confidence="medium",
                           source="manual", original_answer="", grade="", crag_action=""))
        await db.commit()

    res = await eg.list_gaps(status="pending", source="auto_crag", page=1, size=200)
    queries = [g["query"] for g in res["list"]]
    assert q_crag in queries, f"auto_crag 来源记录应在结果中: {queries[:5]}"
    assert q_manual not in queries, "manual 来源记录不应在 auto_crag 过滤结果中"


@pytest.mark.asyncio
async def test_list_gaps_no_source_returns_all():
    """source=None（默认）不过滤，应能取到 auto_crag 与 manual 两种记录。"""
    from datetime import datetime
    from app.services import evidence_gap_service as eg
    from app.db.session import AsyncSessionLocal
    from app.models.evidence_gap import EvidenceGap

    suffix = datetime.now().isoformat().replace(":", "").replace(".", "")
    q_crag = f"no_src_crag_{suffix}"
    q_manual = f"no_src_manual_{suffix}"

    async with AsyncSessionLocal() as db:
        db.add(EvidenceGap(query=q_crag, status="pending", confidence="refused",
                           source="auto_crag", original_answer="", grade="", crag_action=""))
        db.add(EvidenceGap(query=q_manual, status="pending", confidence="medium",
                           source="manual", original_answer="", grade="", crag_action=""))
        await db.commit()

    res = await eg.list_gaps(status="pending", page=1, size=200)
    queries = [g["query"] for g in res["list"]]
    assert q_crag in queries
    assert q_manual in queries


@pytest.mark.asyncio
async def test_list_gaps_backward_compatible_signature():
    """旧调用 list_gaps(status, page, size)（位置参数）仍可工作——签名扩展向后兼容。"""
    from app.services import evidence_gap_service as eg
    res = await eg.list_gaps("pending", 1, 20)
    assert "list" in res and "total" in res
