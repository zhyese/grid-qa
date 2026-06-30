"""答案导出：问答 → Word(.docx) 运维报告，供现场打印归档。

复用 python-docx（已在依赖）。把 问题/答复/引用来源/可信度/安全提示 落成结构化文档。
"""
import io
from datetime import datetime

_CONF_MAP = {
    "high": "✓ 高（证据充分，答案可信）",
    "medium": "⚠ 中（证据有限，部分内容建议人工核对）",
    "refused": "✗ 低（证据不足，已保守处理）",
}


def build_docx(query: str, answer: str, sources: list, meta: dict | None = None) -> bytes:
    """生成运维问答报告 .docx，返回字节流。"""
    from docx import Document

    doc = Document()
    doc.add_heading("电网运维智能问答报告", level=0)
    doc.add_paragraph(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    doc.add_heading("一、问题", level=1)
    doc.add_paragraph(query or "")

    doc.add_heading("二、答复", level=1)
    for line in (answer or "").split("\n"):
        line = line.strip()
        if line:
            doc.add_paragraph(line)

    doc.add_heading("三、引用来源", level=1)
    if sources:
        for i, s in enumerate(sources, 1):
            text = s.get("text", "") if isinstance(s, dict) else str(s)
            doc_name = s.get("docName", "") if isinstance(s, dict) else ""
            doc.add_paragraph(f"[{i}] {doc_name}：{text}".strip("： "), style="List Bullet")
    else:
        doc.add_paragraph("无")

    doc.add_heading("四、可信度", level=1)
    meta = meta or {}
    conf = meta.get("confidence", "")
    doc.add_paragraph(f"置信度：{_CONF_MAP.get(conf, conf or '未评估')}")
    if meta.get("hallucinationRate") is not None:
        doc.add_paragraph(f"幻觉率：{meta['hallucinationRate']}")
    if meta.get("responseTime") is not None:
        doc.add_paragraph(f"耗时：{meta['responseTime']} 秒")

    doc.add_paragraph()
    doc.add_paragraph("⚠ 安全提示：现场操作前必须核对调度指令与安规，本报告仅供辅助参考。")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
