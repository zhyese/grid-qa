"""Chunk 引用元数据新字段 + 迁移幂等。"""
from app.models.chunk import Chunk


def test_chunk_has_citation_meta_fields():
    c = Chunk(doc_id="d1", chunk_idx=0, content="x", section_path="3.1 > 第2条",
              page_num=5, bbox='[10,20,300,80]', table_header="序号|名称",
              metadata_complete=True)
    assert c.section_path == "3.1 > 第2条"
    assert c.page_num == 5
    assert c.bbox == '[10,20,300,80]'
    assert c.table_header == "序号|名称"
    assert c.metadata_complete is True


def test_chunk_fields_default_backward_compat():
    """旧路径不传新字段 → 默认值，向后兼容。"""
    c = Chunk(doc_id="d1", chunk_idx=0, content="x")
    assert c.page_num is None
    assert c.bbox is None
    assert c.section_path == ""
    assert c.table_header == ""
    assert c.metadata_complete is False
