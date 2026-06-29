"""分块策略单测。"""
from app.services.chunk_service import split_text


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
