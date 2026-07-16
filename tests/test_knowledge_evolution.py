"""知识自进化闭环测试。"""
import math
import pytest
from sqlalchemy import select

from app.models.knowledge_evolution import KnowledgeEvolutionDraft
from app.services.knowledge_evolution_service import cluster


def _v(x):
    n = math.sqrt(sum(i * i for i in x))
    return [i / n for i in x]


# ===== T1: 模型 CRUD =====
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


# ===== T3: 聚类 =====
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


# ===== T4: 盲区判定 =====
@pytest.mark.asyncio
async def test_identify_blind_spot_is_blind(monkeypatch):
    from app.services import knowledge_evolution_service as ev
    async def fake_top1(db, q, tenant, top_k=1):
        return [{"score": 0.3, "doc_id": "x"}]
    monkeypatch.setattr(ev, "_retrieve_top1", fake_top1)
    c = {"representative_query": "q", "members": [{"query": "q"}]}
    evi = await ev._identify_blind_spot(None, c, "default")
    assert evi is not None and evi["top1_score"] == 0.3


@pytest.mark.asyncio
async def test_identify_blind_spot_not_blind(monkeypatch):
    from app.services import knowledge_evolution_service as ev
    async def fake_top1(db, q, tenant, top_k=1):
        return [{"score": 0.8, "doc_id": "x"}]
    monkeypatch.setattr(ev, "_retrieve_top1", fake_top1)
    c = {"representative_query": "q", "members": [{"query": "q"}]}
    assert await ev._identify_blind_spot(None, c, "default") is None


# ===== T5: 草稿生成（mock LLM）=====
@pytest.mark.asyncio
async def test_generate_draft(monkeypatch):
    from app.services import knowledge_evolution_service as ev
    async def fake_llm(prompt, model_type):
        return '{"title":"t","content":"c","source_refs":[]}'
    async def fake_docs(db, q, tenant, top_k=3):
        return []
    monkeypatch.setattr(ev, "_call_llm_json", fake_llm)
    monkeypatch.setattr(ev, "_recent_standards", fake_docs)
    c = {"representative_query": "q", "members": [{"query": "q"}, {"query": "q2"}]}
    draft = await ev._generate_draft(None, c, {"top1_score": 0.3}, "default", None)
    assert draft["draft_title"] == "t" and draft["draft_content"] == "c"


# ===== T6: run_scan 编排（mock 管道，验证落库）=====
@pytest.mark.asyncio
async def test_run_scan_persists_drafts(test_db, monkeypatch):
    from app.services import knowledge_evolution_service as ev
    async def fake_extract(db, tenant, since):
        return ["油温高", "油温高1", "油温高2"]
    async def fake_embed(qs):
        return [[1.0, 0.0] for _ in qs]
    def fake_cluster(items, **k):
        return [{"cluster_id": "c1", "representative_query": "油温高", "members": [{"query": "油温高"}]}]
    async def fake_identify(db, c, tenant):
        return {"top1_score": 0.3, "hit_doc_ids": [], "confidence": "medium"}
    async def fake_gen(db, c, evi, tenant, mt):
        return {"draft_title": "t", "draft_content": "c", "source_doc_ids": [], "gap_evidence": evi}
    monkeypatch.setattr(ev, "_extract_dislike", fake_extract)
    monkeypatch.setattr(ev, "_embed", fake_embed)
    monkeypatch.setattr(ev, "cluster", fake_cluster)
    monkeypatch.setattr(ev, "_identify_blind_spot", fake_identify)
    monkeypatch.setattr(ev, "_generate_draft", fake_gen)
    res = await ev.run_scan(test_db, "default", since_hours=168, model_type=None)
    assert res["drafts"] == 1
    rows = (await test_db.execute(select(KnowledgeEvolutionDraft))).scalars().all()
    assert len(rows) == 1 and rows[0].status == "draft"
