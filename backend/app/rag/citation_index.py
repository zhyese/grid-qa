"""第二层 · 服务端受控编号：杜绝 LLM 编造引用编号。

mixed_search 召回后，服务端统一分配本轮局部编号 [1..N] → chunk_id 映射。
编号与 prompt_templates.build_messages_with_history 的 [{i+1}] 天然对齐
（contexts 顺序即编号顺序，i+1 即本模块的 key）。
LLM 只能引用 [1..N]，越界由 citation_verifier（Task 9）剔除。
"""


def build_index(contexts: list[dict]) -> dict[int, str]:
    """位置编号 → chunk_id 映射。

    contexts: mixed_search 返回的 _to_item 产物，每项需含 chunkId
             （由 retrieval_service._to_item 透出，必要时 mixed_search 末尾
               按 (doc_id, chunk_idx) 批量查 Chunk 表回填）。
    返回 {1: chunk_id, 2: chunk_id, ...}，长度 == len(contexts)；
          chunkId 缺失时对应值为空串（校验1 会把空串视为非法引用源）。
    """
    idx: dict[int, str] = {}
    for i, c in enumerate(contexts or []):
        cid = c.get("chunkId") or c.get("chunk_id") or ""
        idx[i + 1] = cid
    return idx


def chunk_id_of(ref_id: int, index: dict[int, str]) -> str:
    """编号 → chunk_id（越界/不存在返回空串，供校验1 判非法引用）。"""
    return index.get(ref_id, "")
