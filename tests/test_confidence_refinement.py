"""置信度/CRAG 标签体系细化（confidence refinement）测试。

T1: evidence_strength 统一函数（v1/v2/rerank降级三路）。
后续 T2/T3... 测试追加于本文件。
"""
from app.rag.crag import evidence_strength


# ===== T1 · evidence_strength =====

def test_evidence_strength_v2_detail_mixed():
    # 2 relevant + 1 partial / 5 → (2 + 0.5) / 5 = 0.5
    assert evidence_strength(detail={"relevant": 2, "partial": 1, "irrelevant": 2, "n": 5}) == 0.5


def test_evidence_strength_v2_all_relevant():
    assert evidence_strength(detail={"relevant": 5, "partial": 0, "irrelevant": 0, "n": 5}) == 1.0


def test_evidence_strength_v2_all_irrelevant():
    assert evidence_strength(detail={"relevant": 0, "partial": 0, "irrelevant": 5, "n": 5}) == 0.0


def test_evidence_strength_v2_partial_only():
    # 0 relevant + 2 partial / 4 → (0 + 1.0) / 4 = 0.25
    assert evidence_strength(detail={"relevant": 0, "partial": 2, "irrelevant": 2, "n": 4}) == 0.25


def test_evidence_strength_v1_top1():
    assert evidence_strength(top1=0.8) == 0.8


def test_evidence_strength_v1_clamp():
    assert evidence_strength(top1=1.5) == 1.0
    assert evidence_strength(top1=-0.2) == 0.0


def test_evidence_strength_rerank_degraded():
    # rerank 未启用/失败 → None（评估器降级，不参与分桶）
    assert evidence_strength(top1=0.9, rerank_ok=False) is None
    assert evidence_strength(detail={"relevant": 5, "partial": 0, "irrelevant": 0, "n": 5},
                             rerank_ok=False) is None


def test_evidence_strength_detail_preferred_over_top1():
    # 同时给 detail 和 top1：detail（v2 逐条）优先
    assert evidence_strength(top1=0.9,
                             detail={"relevant": 0, "partial": 0, "irrelevant": 5, "n": 5}) == 0.0


def test_evidence_strength_n_zero_fallback_top1():
    # detail n=0（异常）→ 回退 top1
    assert evidence_strength(top1=0.7,
                             detail={"relevant": 0, "partial": 0, "irrelevant": 0, "n": 0}) == 0.7


def test_evidence_strength_no_input_none():
    # 啥都没给 → None
    assert evidence_strength() is None


# ===== T2 · 捡回 v2 detail + 归因（断点 A）=====

import pytest
from app.rag import crag as crag_mod


def test_format_crag_reason_with_detail():
    from app.services.qa_service import _format_crag_reason
    r = _format_crag_reason({"relevant": 2, "partial": 1, "irrelevant": 2, "n": 5}, "ambiguous")
    assert "证据有限" in r
    assert "2 relevant" in r and "1 partial" in r and "5 条" in r


def test_format_crag_reason_empty():
    from app.services.qa_service import _format_crag_reason
    assert _format_crag_reason({}, "correct") == ""
    assert _format_crag_reason(None, "correct") == ""


@pytest.mark.asyncio
async def test_crag_correct_v3_picks_up_detail(monkeypatch):
    """CRAG_V3_ENABLE + PERDOC 开 → extras 含 cragDetail/cragReason（断点 A）。"""
    from app.services import qa_service
    from app.rag import crag_v2

    monkeypatch.setattr(qa_service.settings, "CRAG_ENABLE", True)
    monkeypatch.setattr(qa_service.settings, "CRAG_PERDOC_ENABLE", True)
    monkeypatch.setattr(qa_service.settings, "CRAG_V3_ENABLE", True)
    monkeypatch.setattr(qa_service.settings, "RERANK_ENABLE", True)

    detail = {"relevant": 2, "partial": 1, "irrelevant": 2, "n": 5}

    async def _fake_grade(*a, **k):
        return crag_mod.GRADE_CORRECT, detail

    monkeypatch.setattr(crag_v2, "grade_with_llm", _fake_grade)

    _ctxs, conf, _action, grade, extras = await qa_service._crag_correct(
        None, "q", [{"score": 0.9}], "deepseek", 5
    )
    # T4: V3 开时 grade 由 es 统一分桶，es=(2+0.5)/5=0.5 → ambiguous（非 v2 labels 的 correct）
    assert grade == "ambiguous"
    # es=0.5 → medium_high → 老字段 confidence 映射 medium
    assert conf == "medium"
    assert extras["confidenceLabel"] == "medium_high"
    assert extras["confidenceScore"] == 0.5
    assert extras["cragDetail"] == detail
    assert "2 relevant" in extras["cragReason"]


# ===== T4 · v1/v2 口径统一 grade 吃 es（断点 C）=====

from app.rag.crag import grade as crag_grade, GRADE_CORRECT, GRADE_AMBIGUOUS


def test_grade_uses_es_when_provided():
    # es 提供 → 用 es 分桶（忽略 top1）
    assert crag_grade(0.99, 5, True, es=0.5)[0] == GRADE_AMBIGUOUS  # top1 高但 es=0.5
    assert crag_grade(0.1, 5, True, es=0.8)[0] == GRADE_CORRECT     # top1 低但 es=0.8


def test_grade_top1_when_es_none():
    # es=None → 老 top1 逻辑（现状）
    assert crag_grade(0.8, 5, True, es=None)[0] == GRADE_CORRECT


def test_grade_v3_unifies_v1_v2_caliber():
    """同 es 值，v1(top1) 与 v2(detail→es) 给同 grade（口径稳定，断点 C 收口）。

    es 取 0.8：CRAG_HIGH 已从 0.6 调高到 0.72（让更多 medium 进 gap），0.65 会落 ambiguous。
    """
    es_val = 0.8
    g_v1 = crag_grade(es_val, 5, True, es=es_val)
    # v2 detail 算出同 es：16 relevant / 20 = 0.8
    es_v2 = evidence_strength(detail={"relevant": 16, "partial": 0, "irrelevant": 4, "n": 20})
    assert es_v2 == 0.8
    g_v2 = crag_grade(0.0, 20, True, es=es_v2)
    assert g_v1[0] == g_v2[0] == GRADE_CORRECT


# ===== T5 · action×grade 矩阵 + rewriteDelta + refused 归因（断点 D+E）=====

async def _seed_rewrite(monkeypatch, after_score):
    """mock rewrite_query + mixed_search：rewrite 后返回 score=after_score 的 ctx。"""
    from app.services import qa_service, query_rewrite
    async def _fake_rw(q, *a, **k):
        return q + "_改写"
    async def _fake_mixed(*a, **k):
        return [{"score": after_score}]
    monkeypatch.setattr(query_rewrite, "rewrite_query", _fake_rw)
    monkeypatch.setattr(qa_service.retrieval_service, "mixed_search", _fake_mixed)


async def _v3_crag(monkeypatch, initial_score):
    from app.services import qa_service
    for k, v in {"CRAG_ENABLE": True, "CRAG_PERDOC_ENABLE": False,
                 "CRAG_V3_ENABLE": True, "RERANK_ENABLE": True}.items():
        monkeypatch.setattr(qa_service.settings, k, v)
    return await qa_service._crag_correct(None, "q", [{"score": initial_score}], "deepseek", 5)


@pytest.mark.asyncio
async def test_crag_v5_rewrite_recovered(monkeypatch):
    """rewrite 后 correct → rewritten_recovered, rewriteDelta>0, label high。"""
    await _seed_rewrite(monkeypatch, 0.9)
    _c, _conf, action, grade, extras = await _v3_crag(monkeypatch, 0.1)
    assert grade == "correct"
    assert action == "rewritten_recovered"
    assert extras["rewriteDelta"] == 0.8
    assert extras["confidenceLabel"] == "high"
    assert "refusedReason" not in extras


@pytest.mark.asyncio
async def test_crag_v5_rewrite_partial(monkeypatch):
    """rewrite 后 ambiguous → rewritten_partial。"""
    await _seed_rewrite(monkeypatch, 0.5)
    _c, _conf, action, grade, extras = await _v3_crag(monkeypatch, 0.1)
    assert grade == "ambiguous"
    assert action == "rewritten_partial"
    assert extras["rewriteDelta"] == 0.4


@pytest.mark.asyncio
async def test_crag_v5_rewrite_failed_refused(monkeypatch):
    """rewrite 后仍 incorrect → rewritten_failed → refused + refusedReason=rewrite_exhausted。"""
    await _seed_rewrite(monkeypatch, 0.1)
    _c, conf, action, grade, extras = await _v3_crag(monkeypatch, 0.1)
    assert action == "rewritten_failed"
    assert grade == "incorrect"
    assert conf == "refused"
    assert extras["confidenceLabel"] == "refused"
    assert extras["refusedReason"] == "rewrite_exhausted"


def test_refused_reason_classification():
    from app.services.qa_service import _refused_reason
    assert _refused_reason("rewritten_failed", 1, "incorrect") == "rewrite_exhausted"
    assert _refused_reason("normal", 0, "incorrect") == "no_recall"
    assert _refused_reason("normal", 3, "ambiguous") == ""  # 非 refused 场景


# ===== T6 · CRAG 阈值热调 rt_crag_high/low（断点 F）=====

def test_rt_crag_defaults():
    from app.services import config_service
    from app.config import settings
    assert config_service.rt_crag_high() == settings.CRAG_HIGH
    assert config_service.rt_crag_low() == settings.CRAG_LOW


def test_grade_uses_rt_crag_hot_update():
    from app.services import config_service
    from app.rag import crag
    config_service._RUNTIME["crag_high"] = 0.9
    config_service._RUNTIME["crag_low"] = 0.1
    try:
        assert crag.grade(0, 5, True, es=0.5)[0] == "ambiguous"   # 0.1<0.5<0.9
        assert crag.grade(0, 5, True, es=0.95)[0] == "correct"    # ≥0.9
        assert crag.grade(0, 5, True, es=0.05)[0] == "incorrect"  # <0.1
    finally:
        config_service._RUNTIME.pop("crag_high", None)
        config_service._RUNTIME.pop("crag_low", None)


# ===== T7 · 度量 5 指标 + 预注册（可观测）=====

def test_t7_metrics_registered():
    from app.core import metrics
    for name in ("CRAG_EVIDENCE_STRENGTH", "CRAG_CONFIDENCE_LABEL",
                 "CRAG_REFUSED_REASON", "CRAG_REWRITE_DELTA", "OVERCONFIDENT"):
        assert hasattr(metrics, name), f"缺失指标 {name}"


def test_init_metric_series_preregisters_v3_labels():
    from app.core import metrics
    from prometheus_client import generate_latest
    metrics.init_metric_series()  # 不抛
    text = generate_latest().decode()
    assert "grid_crag_confidence_label_total" in text
    assert "grid_crag_refused_reason_total" in text
    assert "grid_crag_evidence_strength" in text


@pytest.mark.asyncio
async def test_crag_v7_emits_metrics(monkeypatch):
    """V3 路径跑通后，CRAG_CONFIDENCE_LABEL 被 observe（refused 路径最易断言）。"""
    from app.services import qa_service
    from prometheus_client import generate_latest
    await _seed_rewrite(monkeypatch, 0.1)  # rewrite 仍 incorrect → refused
    for k, v in {"CRAG_ENABLE": True, "CRAG_PERDOC_ENABLE": False,
                 "CRAG_V3_ENABLE": True, "RERANK_ENABLE": True}.items():
        monkeypatch.setattr(qa_service.settings, k, v)
    await qa_service._crag_correct(None, "q", [{"score": 0.1}], "deepseek", 5)
    text = generate_latest().decode()
    assert "grid_crag_refused_reason_total" in text


# ===== T8 · over_confident 机器人工对齐仲裁（断点 G）=====

@pytest.mark.asyncio
async def test_check_overconfident_disabled_default(monkeypatch):
    from app.services import feedback_optimizer_service as fos
    monkeypatch.setattr(fos.settings, "CONFIDENCE_OVERCONFIDENT_ENABLE", False)
    assert await fos.check_overconfident("q") is False


@pytest.mark.asyncio
async def test_check_overconfident_detects_high_conflict(monkeypatch):
    """dislike 时缓存 confidence=high → 检出 over_confident + 触发 evidence_gap 复核。"""
    from app.services import feedback_optimizer_service as fos
    from app.clients import redis_client as rc
    import app.services.evidence_gap_service as egs

    monkeypatch.setattr(fos.settings, "CONFIDENCE_OVERCONFIDENT_ENABLE", True)

    class _FakeRedis:
        async def scan_iter(self, match=None, count=100):
            yield "qa:default:deepseek:nq:1"

        async def get(self, key):
            import json
            return json.dumps({"confidence": "high", "answer": "ans"})

    monkeypatch.setattr(rc, "get_redis", lambda: _FakeRedis())

    called = []

    async def _fake_collect(*a, **k):
        called.append(a)

    monkeypatch.setattr(egs, "collect", _fake_collect)

    assert await fos.check_overconfident("某query") is True
    assert len(called) == 1  # evidence_gap 复核被调


@pytest.mark.asyncio
async def test_check_overconfident_no_high_skips(monkeypatch):
    """缓存 confidence 非 high → 不检出（无冲突）。"""
    from app.services import feedback_optimizer_service as fos
    from app.clients import redis_client as rc
    import app.services.evidence_gap_service as egs

    monkeypatch.setattr(fos.settings, "CONFIDENCE_OVERCONFIDENT_ENABLE", True)

    class _FakeRedis:
        async def scan_iter(self, match=None, count=100):
            yield "qa:default:deepseek:nq:1"

        async def get(self, key):
            import json
            return json.dumps({"confidence": "medium", "answer": "ans"})

    monkeypatch.setattr(rc, "get_redis", lambda: _FakeRedis())
    called = []
    monkeypatch.setattr(egs, "collect", lambda *a, **k: called.append(a))
    assert await fos.check_overconfident("q") is False
    assert called == []


# ===== T3 · 连续置信度 + 5档 + 修 low（断点 B）=====

from app.rag.crag import confidence_score, label_to_confidence


def test_confidence_score_buckets():
    assert confidence_score(0.9, "normal") == (0.9, "high")
    assert confidence_score(0.6, "normal") == (0.6, "medium_high")
    assert confidence_score(0.4, "normal") == (0.4, "medium_low")
    assert confidence_score(0.25, "normal") == (0.25, "low")        # low 真产出
    assert confidence_score(0.1, "normal") == (0.1, "refused")


def test_confidence_score_refused_action():
    # action=refused（改写后仍 incorrect）→ 强 refused（不论 es）
    assert confidence_score(0.5, "refused") == (0.5, "refused")


def test_confidence_score_degraded_caps_low():
    # rerank 降级（es=None/degraded）→ 封顶 low，绝不进 high
    assert confidence_score(None, "normal", degraded=True) == (0.3, "low")
    assert confidence_score(None, "normal") == (0.3, "low")


def test_confidence_score_boundaries():
    assert confidence_score(0.7, "normal")[1] == "high"
    assert confidence_score(0.5, "normal")[1] == "medium_high"
    assert confidence_score(0.35, "normal")[1] == "medium_low"
    assert confidence_score(0.2, "normal")[1] == "low"


def test_label_to_confidence_legacy_mapping():
    # 老字段兼容：high/refused 保留，medium_*/low → medium（前端零改动）
    assert label_to_confidence("high") == "high"
    assert label_to_confidence("medium_high") == "medium"
    assert label_to_confidence("medium_low") == "medium"
    assert label_to_confidence("low") == "medium"     # low 仅 confidenceLabel 新字段可见
    assert label_to_confidence("refused") == "refused"


@pytest.mark.asyncio
async def test_crag_correct_v3_v1_path_confidence(monkeypatch):
    """V3 开 + v1 路径（无 detail）→ extras 含 confidenceScore/Label/evidenceStrength。"""
    from app.services import qa_service

    monkeypatch.setattr(qa_service.settings, "CRAG_ENABLE", True)
    monkeypatch.setattr(qa_service.settings, "CRAG_PERDOC_ENABLE", False)  # 走 v1
    monkeypatch.setattr(qa_service.settings, "CRAG_V3_ENABLE", True)
    monkeypatch.setattr(qa_service.settings, "RERANK_ENABLE", True)
    # 隔离环境：本机 .env 可能开 SUFFICIENCY_GATE_ENABLE（judge 不可用→降档 high→medium_high），
    # 本用例只测 V3 v1 路径的置信映射，sufficiency gate 必须关。
    monkeypatch.setattr(qa_service.settings, "SUFFICIENCY_GATE_ENABLE", False)

    # top1=0.8 → grade=correct, es=0.8 → high
    _ctxs, conf, _action, grade, extras = await qa_service._crag_correct(
        None, "q", [{"score": 0.8}], "deepseek", 5
    )
    assert grade == "correct"
    assert conf == "high"
    assert extras["confidenceLabel"] == "high"
    assert extras["confidenceScore"] == 0.8
    assert extras["evidenceStrength"] == 0.8
    assert "cragDetail" not in extras  # v1 路径无逐条 detail


@pytest.mark.asyncio
async def test_crag_correct_v3_off_no_extras(monkeypatch):
    """CRAG_V3_ENABLE 关 → extras={} 现状（前端零改动）。"""
    from app.services import qa_service
    from app.rag import crag_v2

    monkeypatch.setattr(qa_service.settings, "CRAG_ENABLE", True)
    monkeypatch.setattr(qa_service.settings, "CRAG_PERDOC_ENABLE", True)
    monkeypatch.setattr(qa_service.settings, "CRAG_V3_ENABLE", False)
    monkeypatch.setattr(qa_service.settings, "RERANK_ENABLE", True)
    # 同上：sufficiency gate 开时会往 extras 塞 answerability/insufficient，与本断言无关
    monkeypatch.setattr(qa_service.settings, "SUFFICIENCY_GATE_ENABLE", False)

    detail = {"relevant": 2, "partial": 1, "irrelevant": 2, "n": 5}

    async def _fake_grade(*a, **k):
        return crag_mod.GRADE_CORRECT, detail

    monkeypatch.setattr(crag_v2, "grade_with_llm", _fake_grade)

    _ctxs, _conf, _action, _grade, extras = await qa_service._crag_correct(
        None, "q", [{"score": 0.9}], "deepseek", 5
    )
    assert extras == {}
