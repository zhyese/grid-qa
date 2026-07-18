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
