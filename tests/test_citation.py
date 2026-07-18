"""证据溯源 auto_cite + 配置项单测 + 可核验引用体系（Task 6-10）单测。

- auto_cite：句级引用回填（点1）。
- citation_index.build_index：服务端受控编号 [1..N] → chunk_id（第二层）。
- judge._verify_claims：NLI 三分类（support/contradict/neutral）（第四层）。
- citation_verifier.verify：三层校验引擎（位置/相似度/NLI）（Task 9）。
- qa_service._apply_citation_verification：串联答案+校验+CRAG 联动（Task 10）。
"""
import asyncio

from app.config import settings
from app.rag import citation
from app.rag.citation_index import build_index, chunk_id_of


def _run(coro):
    return asyncio.run(coro)


def test_citation_settings_defaults():
    # 用 _env_file=None 测纯默认值，避免运行时 .env 覆盖污染断言
    from app.config import Settings
    s = Settings(_env_file=None)
    assert s.CITATION_AUTO_ENABLE is True
    assert s.CITATION_SIM_THRESHOLD == 0.6
    # 新开关默认全 opt-in（关闭=现状）
    assert s.CITATION_VERIFIER_ENABLE is False
    assert s.CITATION_NLI_ENABLE is False
    assert s.CITATION_NLI_TIMEOUT == 5
    assert s.CITATION_STRUCTURED_OUTPUT is False
    assert s.CITATION_REWRITE_ON_FAIL is True
    assert s.CITATION_VERIFY_SIM_THRESHOLD == 0.4  # 校验2专用阈值(独立于 auto_cite 补标的 0.6)


def test_auto_cite_all_already_cited(monkeypatch):
    """答案每句已有角标 → 不再补，trace 全支撑。"""
    async def fake_embed(texts):
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    answer = "主变油温应≤85℃[1]。应申请停运[1]。"
    contexts = [{"chunk": "主变油温限值85", "docName": "A"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[1]" in annotated
    assert trace["totalSupported"] == 2
    assert trace["supportRatio"] == 1.0


def test_auto_cite_bare_sentence_matched(monkeypatch):
    """无角标句子 → 补到最相似 chunk。"""
    calls = []

    async def fake_embed(texts):
        calls.append(texts)
        if len(calls) == 1:           # 第一次：chunks
            return [[1.0, 0.0], [0.0, 1.0]]
        return [[0.9, 0.1]]           # bare 句偏向 chunk0

    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    answer = "油温过高需停运。"        # 无角标
    contexts = [{"chunk": "油温限值", "docName": "A"}, {"chunk": "停运流程", "docName": "B"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[1]" in annotated                   # 补到 chunk0 → [1]（标点前）
    assert trace["supportRatio"] == 1.0


def test_auto_cite_below_threshold_not_annotated(monkeypatch):
    """句子与所有 chunk 相似度都低于阈值 → 不补，保留无引用。"""
    calls = []

    async def fake_embed(texts):
        calls.append(texts)
        if len(calls) == 1:
            return [[1.0, 0.0], [0.9, 0.4359]]   # 两 chunk 近乎同向
        return [[0.0, 1.0]]                       # 句子与两 chunk 都低相关

    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    answer = "完全无关的一句话。"
    contexts = [{"chunk": "x", "docName": "A"}, {"chunk": "y", "docName": "B"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[" not in annotated                   # 没补任何角标
    assert trace["totalSupported"] == 0


def test_auto_cite_embed_failure_degrades(monkeypatch):
    """embed 异常 → 降级，返回原答案 + 仅原有角标 trace，不抛。"""
    async def boom(texts):
        raise RuntimeError("embed down")
    monkeypatch.setattr("app.services.embedding_service.embed_texts", boom)
    answer = "有据[1]。无据句。"
    contexts = [{"chunk": "x", "docName": "A"}]
    annotated, trace = _run(citation.auto_cite(answer, contexts, threshold=0.6))
    assert "[1]" in annotated
    assert trace["totalSupported"] == 1           # 只有原本带角标那句
    assert trace["totalSentences"] == 2


def test_auto_cite_empty_contexts():
    """无 contexts → 原样返回，不调 embed。"""
    annotated, trace = _run(citation.auto_cite("某句。", [], threshold=0.6))
    assert "[" not in annotated
    assert trace["totalSupported"] == 0


# ---------- Task 6: citation_index 服务端受控编号 ----------


def test_build_index_maps_position_to_chunk_id():
    """build_index：位置编号 [1..N] → chunk_id，与 prompt [i+1] 对齐。"""
    contexts = [
        {"chunkId": "c1", "chunk": "油温限值", "docName": "A"},
        {"chunkId": "c2", "chunk": "停运流程", "docName": "B"},
    ]
    idx = build_index(contexts)
    assert idx == {1: "c1", 2: "c2"}
    assert idx[1] == "c1"


def test_build_index_empty():
    assert build_index([]) == {}


def test_chunk_id_of_returns_empty_for_out_of_range():
    """越界编号返回空串（供校验1 判非法引用）。"""
    idx = {1: "c1", 2: "c2"}
    assert chunk_id_of(1, idx) == "c1"
    assert chunk_id_of(3, idx) == ""
    assert chunk_id_of(0, idx) == ""


# ---------- Task 7: 结构化 schema + 降级解析 ----------


def test_citation_answer_schema_parse():
    """结构化 JSON 输出 → CitationAnswer。"""
    from app.schemas.citation import CitationAnswer
    raw = {
        "answer_text": "主变油温应≤85℃[1]。",
        "citation_map": [{"sentence": "主变油温应≤85℃", "ref_id": 1, "chunk_id": "c1",
                          "metadata": {"doc_title": "A", "section_path": "3.1", "page_num": 3}}],
        "unverified_claim": [],
    }
    ans = CitationAnswer(**raw)
    assert ans.answer_text.endswith("[1]。")
    assert ans.citation_map[0].ref_id == 1
    assert ans.unverified_claim == []


def test_parse_citation_answer_degrades_on_plain_text():
    """LLM 纯文本输出（无 JSON）→ 降级：answer_text=原文，citation_map 走 evidence_trace 反查。"""
    from app.schemas.citation import parse_citation_answer
    ans = parse_citation_answer("主变油温应≤85℃[1]。", index={1: "c1"})
    assert ans.answer_text == "主变油温应≤85℃[1]。"
    # 降级路径：[1] 反查到 c1
    assert any(c.ref_id == 1 and c.chunk_id == "c1" for c in ans.citation_map)


# ---------- Task 8: judge._verify_claims NLI 三分类 ----------


def test_verify_claims_three_way(monkeypatch):
    """_verify_claims 三分类：support / contradict / neutral。"""
    async def fake_chat(messages, temperature=0, max_tokens=800):
        # 模拟 LLM 返回逐条判定
        return '{"claims":[{"text":"油温限值85度","label":"support"},{"text":"核辐射免责","label":"contradict"},{"text":"背景介绍","label":"neutral"}]}'
    monkeypatch.setattr("app.providers.factory.get_llm_provider",
                        lambda mt: type("P", (), {"chat": staticmethod(fake_chat)})())
    from app.rag import judge
    res = _run(judge._verify_claims(["油温限值85度", "核辐射免责", "背景介绍"], ["资料A"], "deepseek"))
    labels = [r["label"] for r in res]
    assert labels == ["support", "contradict", "neutral"]


def test_judge_hallucination_still_works_after_extract(monkeypatch):
    """_verify_claims 新增后，judge_hallucination 仍可调用且返回结构完整（零回归守护）。

    mock 格式必须匹配 judge_hallucination 内部期望（supported + counts），非 _verify_claims 的 label 格式。
    """
    async def fake_chat(messages, temperature=0, max_tokens=800):
        return '{"claims":[{"text":"油温限值85","supported":true}],"supported_count":1,"total_count":1}'
    monkeypatch.setattr("app.providers.factory.get_llm_provider",
                        lambda mt: type("P", (), {"chat": staticmethod(fake_chat)})())
    from app.rag import judge
    res = _run(judge.judge_hallucination("油温限值85", ["资料A"], "deepseek"))
    assert res["supported_ratio"] == 1.0  # 1/1 support
    assert "hallucination" in res


# ---------- Task 9: citation_verifier 三层校验引擎 ----------


def test_verify_check1_drops_out_of_range_ref(monkeypatch):
    """校验1：ref_id 越界（不在 index）→ drop。"""
    async def fake_embed(texts):  # 校验2 放行（同向量 → cosine=1）
        return [[1.0] for _ in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    from app.rag.citation_verifier import verify
    from app.schemas.citation import CitationItem
    cmap = [CitationItem(sentence="s1", ref_id=1, chunk_id="c1"),
            CitationItem(sentence="s2", ref_id=99, chunk_id="")]  # 99 越界
    res = _run(verify("s1[1] s2[99]", cmap, {1: "c1"}, [{"chunkId": "c1", "chunk": "x"}], "deepseek",
                      nli_enable=False))
    assert 99 in res.dropped_refs
    keep = [i for i in res.items if i.action == "keep"]
    assert any(i.ref_id == 1 for i in keep)


def test_verify_check2_drops_low_similarity(monkeypatch):
    """校验2：句 vs chunk cosine < 0.6 → drop。"""
    # 造差异化：句 "完全无关句" → [0,1]，chunk "x" → [1,0]（正交，cosine=0）
    async def fake_embed2(texts):
        return [[0.0, 1.0] if "无关" in t else [1.0, 0.0] for t in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed2)
    from app.rag.citation_verifier import verify
    from app.schemas.citation import CitationItem
    cmap = [CitationItem(sentence="完全无关句", ref_id=1, chunk_id="c1")]
    res = _run(verify("完全无关句[1]", cmap, {1: "c1"}, [{"chunkId": "c1", "chunk": "x"}], "deepseek",
                      nli_enable=False))
    assert res.items[0].action == "drop"


def test_verify_nli_contradict_drops(monkeypatch):
    """校验3：NLI 判 contradict → drop。"""
    async def fake_verify(claims, sources, model_type=None):
        return [{"text": c, "label": "contradict"} for c in claims]
    monkeypatch.setattr("app.rag.judge._verify_claims", fake_verify)
    async def fake_embed(texts):  # 校验2 放行
        return [[1.0] for _ in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    from app.rag.citation_verifier import verify
    from app.schemas.citation import CitationItem
    cmap = [CitationItem(sentence="核辐射免责", ref_id=1, chunk_id="c1")]
    res = _run(verify("核辐射免责[1]", cmap, {1: "c1"}, [{"chunkId": "c1", "chunk": "核辐射不在保障范围"}], "deepseek",
                      nli_enable=True))
    assert res.items[0].nli_label == "contradict"
    assert res.items[0].action == "drop"
    assert 1 in res.dropped_refs


# ---------- Task 10: qa_service._apply_citation_verification 串联 ----------


def test_apply_citation_verification_disabled_returns_empty(monkeypatch):
    """CITATION_VERIFIER_ENABLE=False → (ans, {})，零字段、零破坏。"""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "CITATION_VERIFIER_ENABLE", False)
    from app.services.qa_service import _apply_citation_verification
    ans, extras = _run(_apply_citation_verification(
        "主变油温应≤85℃[1]。", [{"chunkId": "c1", "chunk": "油温85", "docName": "A"}], "deepseek"))
    assert ans == "主变油温应≤85℃[1]。"
    assert extras == {}


def test_apply_citation_verification_enabled_includes_fields(monkeypatch):
    """CITATION_VERIFIER_ENABLE=True → extras 含 citationVerified/citationIndex/citationMap。"""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "CITATION_VERIFIER_ENABLE", True)
    monkeypatch.setattr(cfg.settings, "CITATION_NLI_ENABLE", False)
    # mock verify：返回 keep（校验放行，不打真实 NLI/embed）
    async def fake_verify(answer_text, cmap, index, contexts, mt, *, nli_enable=None):
        from app.schemas.citation import VerifyItem, VerifyResult
        return VerifyResult(items=[VerifyItem(ref_id=1, chunk_id="c1", valid=True,
                                              nli_label="unknown", action="keep")])
    monkeypatch.setattr("app.rag.citation_verifier.verify", fake_verify)
    from app.services.qa_service import _apply_citation_verification
    ans, extras = _run(_apply_citation_verification(
        "主变油温应≤85℃[1]。", [{"chunkId": "c1", "chunk": "主变油温不超过85度", "docName": "A"}], "deepseek"))
    assert "citationVerified" in extras
    assert "citationIndex" in extras
    assert extras["citationIndex"] == {1: "c1"}            # build_index 位置→chunk_id
    assert ans == "主变油温应≤85℃[1]。"                       # 无 drop，答案不变
