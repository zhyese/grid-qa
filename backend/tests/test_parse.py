"""parse_service 引用元数据：PDF page_num+bbox / Excel·Word table_header。"""
import pytest


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
