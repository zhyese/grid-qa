"""split_structured 透传引用元数据（page_num/bbox/table_header/section_path）。"""
from app.services import chunk_service


def test_split_structured_passes_citation_meta():
    """split_structured 透传 sections 的 page_num/bbox/table_header 到 chunk。"""
    sections = [
        {"type": "text", "content": "主变油温应不超过85度，超过需申请停运。", "page_num": 3, "bbox": "[1,2,3,4]"},
        {"type": "table", "content": "| 序号 | 限值 |\n|---|---|\n| 1 | 85 |", "table_header": "序号 | 限值"},
    ]
    chunks = chunk_service.split_structured(sections, parent_size=2000, child_size=500)
    text_chunk = next(c for c in chunks if c["chunk_type"] == "child")
    assert text_chunk["page_num"] == 3
    assert text_chunk["bbox"] == "[1,2,3,4]"
    table_chunk = next(c for c in chunks if c["chunk_type"] == "table")
    assert table_chunk["table_header"] == "序号 | 限值"
