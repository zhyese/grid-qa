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
