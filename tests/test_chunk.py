"""分块策略单测（含结构感知分块 + parent-child）。"""
from collections import Counter

from app.services.chunk_service import split_structured, split_text


def test_empty():
    assert split_text("") == []


def test_short_one_chunk():
    assert split_text("短文本不分块") == ["短文本不分块"]


def test_long_split_at_sentence_boundary():
    text = "检查油位油温是否正常。" * 80  # 超长，重复整句
    chunks = split_text(text, chunk_size=100, overlap=20)
    assert len(chunks) > 1
    # 非末块应在句末 "。" 断开（不切半句）
    for c in chunks[:-1]:
        assert c.rstrip().endswith("。") or len(c) <= 100


def test_paragraph_accumulation():
    text = "短段落一。\n短段落二。\n短段落三。"
    chunks = split_text(text, chunk_size=500, overlap=80)
    # 三个短段落累积成一块
    assert len(chunks) == 1
    assert "短段落一" in chunks[0] and "短段落三" in chunks[0]


# ---------- 结构感知分块 + Parent-Child ----------


def test_split_structured_empty():
    assert split_structured([]) == []
    assert split_structured([{"type": "text", "content": ""}]) == []


def test_split_structured_table_kept_intact():
    """表格段整体成一块，不被字符切分（结构保留）。"""
    table_md = "| 设备 | 状态 |\n|---|---|\n| 主变压器 | 正常 |"
    chunks = split_structured([{"type": "table", "content": table_md}])
    assert len(chunks) == 1
    assert chunks[0]["chunk_type"] == "table"
    assert "主变压器" in chunks[0]["text"]
    assert "|" in chunks[0]["text"]  # markdown 表格结构保留


def test_split_structured_parent_child_grouping():
    """正文大段切成多个子块，同一父块内子块共享 parent_idx（small-to-big 基础）。"""
    text = "步骤一：检查油位油温是否正常。\n" * 200  # 远超父块窗口
    chunks = split_structured(
        [{"type": "text", "content": text}], parent_size=300, child_size=100, overlap=10
    )
    assert len(chunks) > 1
    for c in chunks:
        assert c["chunk_type"] in ("child", "table")
        assert "parent_idx" in c
    # 至少有一个父块组含多个子块（证明父子聚合有意义，非每块自成一组）
    parent_counts = Counter(c["parent_idx"] for c in chunks)
    assert any(v > 1 for v in parent_counts.values())


def test_split_structured_table_and_text_separate_groups():
    """表格与正文分属不同父块组（各自独立，parent_idx 递增）。"""
    chunks = split_structured([
        {"type": "table", "content": "| a | b |\n|---|---|\n| 1 | 2 |"},
        {"type": "text", "content": "正文内容一。"},
    ])
    assert len(chunks) == 2
    assert chunks[0]["parent_idx"] != chunks[1]["parent_idx"]


# ---------- Task 4: 引用元数据透传（page_num/bbox/table_header/section_path） ----------


def test_split_structured_passes_citation_meta():
    """split_structured 透传 sections 的 page_num/bbox/table_header 到 chunk。"""
    sections = [
        {"type": "text", "content": "主变油温应不超过85度，超过需申请停运。", "page_num": 3, "bbox": "[1,2,3,4]"},
        {"type": "table", "content": "| 序号 | 限值 |\n|---|---|\n| 1 | 85 |", "table_header": "序号 | 限值"},
    ]
    chunks = split_structured(sections, parent_size=2000, child_size=500)
    text_chunk = next(c for c in chunks if c["chunk_type"] == "child")
    assert text_chunk["page_num"] == 3
    assert text_chunk["bbox"] == "[1,2,3,4]"
    table_chunk = next(c for c in chunks if c["chunk_type"] == "table")
    assert table_chunk["table_header"] == "序号 | 限值"
