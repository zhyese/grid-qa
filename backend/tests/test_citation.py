"""可核验 RAG 引用体系 · 单测。

- citation_index.build_index：服务端受控编号 [1..N] → chunk_id（第二层）。
  位置编号与 prompt_templates.build_messages_with_history 的 [{i+1}] 天然对齐。
"""
from app.rag.citation_index import build_index, chunk_id_of


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
