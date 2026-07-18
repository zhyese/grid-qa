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


# ---------- Task 3: 引用元数据（PDF page_num+bbox / Excel·Word table_header） ----------


def test_extract_pdf_structured_has_page_num():
    """PDF 结构化解析 → section 带 page_num（首字符 bbox）。"""
    from app.services import parse_service
    # 造一个极简 PDF：用 reportlab 无则跳过；有则断言 page_num
    try:
        import reportlab  # noqa: F401
    except ImportError:
        pytest.skip("reportlab 未装，跳过 PDF 造样")
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas
    import io

    # reportlab 默认 Helvetica 不含中文，需注册 Adobe CJK 内置字体
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("STSong-Light", 14)
    # 多写一行：单行 9 字会撞到 is_scanned 阈值（<10字/页判扫描件）
    c.drawString(100, 750, "主变油温限值85度")
    c.drawString(100, 730, "超过报警阈值应立即处置")
    c.showPage()
    c.save()
    sections, is_scanned = parse_service.extract_pdf_structured(buf.getvalue())
    assert not is_scanned
    text_secs = [s for s in sections if s["type"] == "text"]
    assert text_secs and text_secs[0]["page_num"] == 1
    assert isinstance(text_secs[0].get("bbox"), str)  # JSON 串


def test_extract_xlsx_has_table_header():
    """Excel → table 段带 table_header（首行）。"""
    from app.services import parse_service
    import openpyxl
    import io

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["序号", "设备", "限值"])
    ws.append(["1", "主变", "85"])
    buf = io.BytesIO()
    wb.save(buf)
    sections = parse_service.extract_xlsx(buf.getvalue())
    assert sections and sections[0]["type"] == "table"
    assert "序号" in sections[0]["table_header"]
