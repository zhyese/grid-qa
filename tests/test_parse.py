"""结构化解析单测（表格转 markdown + 路由，纯逻辑不依赖重型解析库）。"""
import pytest

from app.services.parse_service import _table_to_markdown, parse_file_structured


def test_table_to_markdown_basic():
    rows = [["设备", "状态"], ["主变压器", "正常"], ["断路器", "故障"]]
    md = _table_to_markdown(rows)
    lines = md.split("\n")
    assert len(lines) == 4  # 表头 + 分隔 + 2 数据行
    assert lines[0] == "| 设备 | 状态 |"
    assert lines[1] == "|---|---|"
    assert "主变压器" in lines[2] and "断路器" in lines[3]


def test_table_to_markdown_filters_empty_rows():
    md = _table_to_markdown([["a", ""], ["", ""], ["", "b"]])
    # 全空行被过滤，只剩表头 + 一行含 b 的数据
    assert "b" in md
    assert md.count("\n") == 2


def test_table_to_markdown_empty():
    assert _table_to_markdown([]) == ""
    assert _table_to_markdown([["", ""]]) == ""


def test_table_to_markdown_pads_ragged_rows():
    """不等长行补齐列宽，避免破坏 markdown 表格结构。"""
    md = _table_to_markdown([["a", "b", "c"], ["1", "2"]])
    for line in md.split("\n"):
        assert line.count("|") == 4  # 每行 3 列 → 4 个 |


def test_parse_structured_unsupported_raises():
    with pytest.raises(ValueError):
        parse_file_structured("a.xyz", b"")


def test_parse_structured_txt():
    sections, scanned = parse_file_structured("a.txt", "检查油位正常".encode("utf-8"))
    assert scanned is False
    assert len(sections) == 1
    assert sections[0]["type"] == "text"
    assert "油位" in sections[0]["content"]


def test_parse_structured_image_marks_scanned():
    """图片走 OCR 路径（is_scanned=True，sections 空，由调用方触发 OCR）。"""
    sections, scanned = parse_file_structured("scan.jpg", b"\x00")
    assert scanned is True
    assert sections == []
