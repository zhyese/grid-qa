"""知识自进化闭环测试。"""
import pytest
from sqlalchemy import select

from app.models.knowledge_evolution import KnowledgeEvolutionDraft


@pytest.mark.asyncio
async def test_draft_create_and_read(test_db):
    """模型 CRUD + 默认值（status/quality_score 默认）。"""
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
    assert row.status == "draft"          # default
    assert row.quality_score == 0.6       # default
    assert row.cluster_id == "c1"
    assert row.member_queries_json == '["主变压器油温高怎么处理"]'
