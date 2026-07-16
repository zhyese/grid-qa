"""知识自进化闭环测试。"""
import math
import pytest
from sqlalchemy import select

from app.models.knowledge_evolution import KnowledgeEvolutionDraft
from app.services.knowledge_evolution_service import cluster


def _v(x):
    n = math.sqrt(sum(i * i for i in x))
    return [i / n for i in x]


# ===== T1: 模型 CRUD + 默认值 =====
@pytest.mark.asyncio
async def test_draft_create_and_read(test_db):
    d = KnowledgeEvolutionDraft(
        id="d1", tenant_id="default", cluster_id="c1",
        representative_query="主变压器油温高怎么处理",
        member_queries_json='["主变压器油温高怎么处理"]',
    )
    test_db.add(d)
    await test_db.commit()
    row = (await test_db.execute(
        select(KnowledgeEvolutionDraft).where(KnowledgeEvolutionDraft.id == "d1")
    )).scalar_one()
    assert row.status == "draft"
    assert row.quality_score == 0.6
    assert row.cluster_id == "c1"


# ===== T3: 零依赖贪心近邻聚类 =====
def test_cluster_groups_similar():
    items = [
        {"query": "油温高", "vec": _v([1, 0.01])},
        {"query": "油温报警", "vec": _v([1, 0.02])},
        {"query": "油温高", "vec": _v([1, 0.015])},
        {"query": "SF6漏气", "vec": _v([0.01, 1])},
    ]
    clusters = cluster(items, threshold=0.95, min_size=2)
    assert len(clusters) == 1
    assert clusters[0]["representative_query"] in ("油温高", "油温报警")


def test_cluster_filters_small():
    assert cluster([{"query": "x", "vec": [1, 0]}], threshold=0.5, min_size=3) == []


# ===== T4: 盲区判定（mock _retrieve_top1）=====
@pytest.mark.asyncio
async def test_identify_blind_spot_is_blind(monkeypatch):
    from app.services import knowledge_evolution_service as ev
    async def fake_top1(db, q, tenant, top_k=1):
        return [{"score": 0.3, "doc_id": "x"}]   # < 0.55 = 盲区
    monkeypatch.setattr(ev, "_retrieve_top1", fake_top1)
    c = {"representative_query": "q", "members": [{"query": "q"}]}
    evi = await ev._identify_blind_spot(None, c, "default")
    assert evi is not None and evi["top1_score"] == 0.3


@pytest.mark.asyncio
async def test_identify_blind_spot_not_blind(monkeypatch):
    from app.services import knowledge_evolution_service as ev
    async def fake_top1(db, q, tenant, top_k=1):
        return [{"score": 0.8, "doc_id": "x"}]   # >= 0.55 = 非盲区
    monkeypatch.setattr(ev, "_retrieve_top1", fake_top1)
    c = {"representative_query": "q", "members": [{"query": "q"}]}
    assert await ev._identify_blind_spot(None, c, "default") is None
