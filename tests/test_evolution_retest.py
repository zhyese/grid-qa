"""B5: 草稿回流回测（run_scan 入口对 indexed 草稿重跑 member_queries 算 lift）。

隔离策略（Phase 1 教训）：
- pytestmark integration：连真实 MySQL（SQLite 与 MySQL 在 datetime/JSON 行为有差异，Phase 1
  feedback_fix_rate 同口径走 integration）。
- 唯一 representative_query + 唯一 tenant_id：聚合查询不命中其他测试残留。
- setup/teardown 精确清理：按 tenant_id 删本测试插入的草稿行，不污染共享库。
"""
import json
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core import metrics
from app.db.session import AsyncSessionLocal
from app.models.knowledge_evolution import KnowledgeEvolutionDraft
from app.services import knowledge_evolution_service as ev
from app.services.task_queue_service import utcnow

pytestmark = pytest.mark.integration  # 依赖容器真 DB（MySQL），CI 无 DB 跳过


@pytest_asyncio.fixture
async def unique_tenant():
    """唯一 tenant_id + setup/teardown 精确清理（按 tenant_id 删本测试草稿行）。"""
    tenant = f"t-b5-{uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        await db.execute(delete(KnowledgeEvolutionDraft).where(
            KnowledgeEvolutionDraft.tenant_id == tenant))
        await db.commit()

    yield tenant

    async with AsyncSessionLocal() as db:
        await db.execute(delete(KnowledgeEvolutionDraft).where(
            KnowledgeEvolutionDraft.tenant_id == tenant))
        await db.commit()


def _make_draft(draft_id, tenant, *, indexed_at, top1_score=0.3,
                member_queries=None, quality_score=0.6):
    """构造一条 status=indexed 的草稿（gap_evidence_json 已含回流前基线 top1_score）。"""
    return KnowledgeEvolutionDraft(
        id=draft_id, tenant_id=tenant, cluster_id="c", representative_query=f"rq-{draft_id}",
        member_queries_json=json.dumps(member_queries or [f"q-{draft_id}"], ensure_ascii=False),
        gap_evidence_json=json.dumps(
            {"top1_score": top1_score, "hit_doc_ids": [], "confidence": "medium"},
            ensure_ascii=False),
        status="indexed", quality_score=quality_score, indexed_at=indexed_at,
    )


@pytest.mark.asyncio
async def test_retest_lift_positive_writes_back(unique_tenant, monkeypatch):
    """回流后检索分数提升（0.3→0.8）→ lift≈0.5 写回 gap_evidence_json，EVOLUTION_LIFT observe。"""
    tenant = unique_tenant
    old = utcnow() - timedelta(days=30)   # 早于默认 AFTER_DAYS=7，触发回测
    async with AsyncSessionLocal() as db:
        db.add(_make_draft("b5-pos", tenant, indexed_at=old, top1_score=0.3,
                           member_queries=["q1", "q2"]))
        await db.commit()

    observed = []
    async def fake_top1(db, q, t, top_k=1):
        return [{"score": 0.8, "doc_id": f"ai-evo-{t}"}]
    monkeypatch.setattr(ev, "_retrieve_top1", fake_top1)
    monkeypatch.setattr(metrics.EVOLUTION_LIFT, "observe",
                        lambda x: observed.append(x))

    async with AsyncSessionLocal() as db:
        n = await ev._retest_indexed_drafts(db, tenant)
        assert n == 1
        row = (await db.execute(
            select(KnowledgeEvolutionDraft).where(
                KnowledgeEvolutionDraft.id == "b5-pos")
        )).scalar_one()
        evi = json.loads(row.gap_evidence_json)
        assert evi["top1_score"] == 0.3            # 原基线保留
        assert evi["after_score"] == 0.8
        assert evi["lift"] == round(0.8 - 0.3, 3)  # 0.5
        assert "retested_at" in evi
        # lift>0：quality_score 不下调
        assert row.quality_score == 0.6
    assert 0.5 in observed


@pytest.mark.asyncio
async def test_retest_zero_recall_no_downgrade(unique_tenant, monkeypatch):
    """零召回（top1 命中非 AI 草稿文档）→ after=0.0 → 不降权（可能是检索故障而非草稿无效）。

    I2 修复：after==0.0 时跳过降权，防批量误判。
    """
    tenant = unique_tenant
    old = utcnow() - timedelta(days=30)
    async with AsyncSessionLocal() as db:
        db.add(_make_draft("b5-zero", tenant, indexed_at=old, top1_score=0.3,
                           member_queries=["q1"]))
        await db.commit()

    async def fake_top1(db, q, t, top_k=1):
        return [{"score": 0.2, "doc_id": "d"}]  # doc_id != ai-evo-{tenant} → 零召回
    monkeypatch.setattr(ev, "_retrieve_top1", fake_top1)
    monkeypatch.setattr(metrics.EVOLUTION_LIFT, "observe", lambda x: None)

    async with AsyncSessionLocal() as db:
        n = await ev._retest_indexed_drafts(db, tenant)
        assert n == 1
        row = (await db.execute(
            select(KnowledgeEvolutionDraft).where(
                KnowledgeEvolutionDraft.id == "b5-zero")
        )).scalar_one()
        evi = json.loads(row.gap_evidence_json)
        assert evi["after_score"] == 0.0
        assert evi["lift"] == round(0.0 - 0.3, 3)   # -0.3
        # after==0.0（零召回）→ 不降权
        assert row.quality_score == 0.6


@pytest.mark.asyncio
async def test_retest_low_lift_with_recall_downgrades_quality(unique_tenant, monkeypatch):
    """有召回但 lift≤0（top1 命中 AI 草稿但分数低于基线）→ 降权。

    I2 修复：只有 after>0 且 lift≤0 才降权（回流确实无效）。
    """
    tenant = unique_tenant
    old = utcnow() - timedelta(days=30)
    async with AsyncSessionLocal() as db:
        db.add(_make_draft("b5-low", tenant, indexed_at=old, top1_score=0.5,
                           member_queries=["q1"]))
        await db.commit()

    async def fake_top1(db, q, t, top_k=1):
        return [{"score": 0.3, "doc_id": f"ai-evo-{tenant}"}]  # 命中 AI 草稿但分数低
    monkeypatch.setattr(ev, "_retrieve_top1", fake_top1)
    monkeypatch.setattr(metrics.EVOLUTION_LIFT, "observe", lambda x: None)

    async with AsyncSessionLocal() as db:
        n = await ev._retest_indexed_drafts(db, tenant)
        assert n == 1
        row = (await db.execute(
            select(KnowledgeEvolutionDraft).where(
                KnowledgeEvolutionDraft.id == "b5-low")
        )).scalar_one()
        evi = json.loads(row.gap_evidence_json)
        assert evi["after_score"] == 0.3
        assert evi["lift"] == round(0.3 - 0.5, 3)   # -0.2
        # after>0 且 lift≤0 → 降权
        assert row.quality_score == round(0.6 * 0.5, 3)  # 0.3


@pytest.mark.asyncio
async def test_retest_skips_recent_indexed(unique_tenant, monkeypatch):
    """indexed_at 在 AFTER_DAYS(7) 内 → 跳过不回测（给回流留生效窗口）。"""
    tenant = unique_tenant
    recent = utcnow() - timedelta(days=1)   # 1 天前 < 7 天阈值
    async with AsyncSessionLocal() as db:
        db.add(_make_draft("b5-recent", tenant, indexed_at=recent, top1_score=0.3))
        await db.commit()

    called = []
    async def fake_top1(db, q, t, top_k=1):
        called.append(q)
        return [{"score": 0.99, "doc_id": "d"}]
    monkeypatch.setattr(ev, "_retrieve_top1", fake_top1)
    monkeypatch.setattr(metrics.EVOLUTION_LIFT, "observe", lambda x: None)

    async with AsyncSessionLocal() as db:
        n = await ev._retest_indexed_drafts(db, tenant)
        assert n == 0
        assert called == []   # 未触发检索
        row = (await db.execute(
            select(KnowledgeEvolutionDraft).where(
                KnowledgeEvolutionDraft.id == "b5-recent")
        )).scalar_one()
        # gap_evidence_json 未被回测改写（无 after_score/lift）
        evi = json.loads(row.gap_evidence_json)
        assert "after_score" not in evi and "lift" not in evi
        assert row.quality_score == 0.6   # 未降权


@pytest.mark.asyncio
async def test_retest_switch_disable(unique_tenant, monkeypatch):
    """EVOLUTION_RETEST_ENABLE=False → 守关直接返回 0，不查不写。"""
    tenant = unique_tenant
    old = utcnow() - timedelta(days=30)
    async with AsyncSessionLocal() as db:
        db.add(_make_draft("b5-off", tenant, indexed_at=old, top1_score=0.3))
        await db.commit()

    from app.config import settings
    monkeypatch.setattr(settings, "EVOLUTION_RETEST_ENABLE", False)

    called = []
    async def fake_top1(db, q, t, top_k=1):
        called.append(q)
        return [{"score": 0.99, "doc_id": "d"}]
    monkeypatch.setattr(ev, "_retrieve_top1", fake_top1)
    monkeypatch.setattr(metrics.EVOLUTION_LIFT, "observe", lambda x: None)

    async with AsyncSessionLocal() as db:
        n = await ev._retest_indexed_drafts(db, tenant)
        assert n == 0
        assert called == []


@pytest.mark.asyncio
async def test_run_scan_invokes_retest(unique_tenant, monkeypatch):
    """run_scan 入口先调 _retest_indexed_drafts（接线测试）。

    防回归：若有人误删 run_scan 中的 `await _retest_indexed_drafts(db, tenant)`，
    该测可发现——即使 fixture 无 dislike（run_scan 在 _extract_dislike 返回空时
    提前 return，但 retest 在入口最先调用，必须先于 dislike 抽取执行）。
    仿 B3 test_run_scan_invokes_aggregation 模式：spy 替换 _retest_indexed_drafts
    记录调用次数，调 run_scan 后断言 spy 被调用。
    """
    tenant = unique_tenant

    calls = []
    async def spy_retest(db, t):
        calls.append(t)
        return 0   # 不实际回测，只验证接线

    monkeypatch.setattr(ev, "_retest_indexed_drafts", spy_retest)
    # _extract_dislike 返回空 → run_scan 在 retest 之后提前 return，进一步隔离
    async def fake_extract_dislike(*a, **kw):
        return []
    monkeypatch.setattr(ev, "_extract_dislike", fake_extract_dislike)

    async with AsyncSessionLocal() as db:
        result = await ev.run_scan(db, tenant, since_hours=168, model_type=None)

    assert calls == [tenant]            # run_scan 入口调过 retest，且 tenant 透传正确
    assert result == {"clusters": 0, "drafts": 0}
