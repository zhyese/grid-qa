"""可核验 RAG 引用体系 · 单测。

- citation_index.build_index：服务端受控编号 [1..N] → chunk_id（第二层）。
  位置编号与 prompt_templates.build_messages_with_history 的 [{i+1}] 天然对齐。
- judge._verify_claims：NLI 三分类（support/contradict/neutral），校验3 核心（第四层）。
"""
import asyncio

from app.rag.citation_index import build_index, chunk_id_of

_run = asyncio.run


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
