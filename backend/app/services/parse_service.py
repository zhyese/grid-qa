"""文档解析：数字文档文本提取 + 扫描件/图片 OCR。

OCR 引擎说明：Windows + paddlepaddle 3.3.1 原生 PaddleOCR 存在 onednn PIR
`ConvertPirAttribute2RuntimeAttribute` 未实现的引擎 bug（实测关闭 oneDNN / 关闭 PIR
新执行器 / monkey-patch enable_mkldnn 均无效，属性内嵌于模型）。故改用
rapidocr-onnxruntime —— 基于 PaddleOCR 官方 PP-OCR 模型、onnxruntime 推理后端，
识别效果一致。生产 Linux/Docker 环境可切回原生 paddleocr（无此 bug）。
"""
import io
from typing import Tuple

_OCR_INSTANCE = None


def _get_ocr():
    global _OCR_INSTANCE
    if _OCR_INSTANCE is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR_INSTANCE = RapidOCR()
    return _OCR_INSTANCE


def _ext(name: str) -> str:
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


# ---------- 数字文档 ----------


def extract_pdf(content: bytes) -> Tuple[str, bool]:
    """每页平均文本<10字 → 判定扫描件，走 OCR。"""
    import pdfplumber

    parts, n = [], 0
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        n = len(pdf.pages)
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    text = "\n".join(parts).strip()
    is_scanned = n > 0 and len(text) < n * 10
    return text, is_scanned


def extract_docx(content: bytes) -> str:
    from docx import Document

    return "\n".join(p.text for p in Document(io.BytesIO(content)).paragraphs).strip()


def extract_txt(content: bytes) -> str:
    for enc in ("utf-8", "gbk", "gb2312"):
        try:
            return content.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore").strip()


# ---------- 扫描件 / 图片 OCR（rapidocr-onnxruntime）----------


def _extract_ocr_texts(result) -> list:
    """rapidocr 返回 (result, elapse)；result=list[[box,text,score]] 或 None。"""
    texts = []
    if not result:
        return texts
    for item in result:
        if item and len(item) >= 2:
            texts.append(item[1])
    return texts


def _ocr_ndarray(img) -> str:
    result, _elapse = _get_ocr()(img)
    return "\n".join(_extract_ocr_texts(result))


def ocr_image(content: bytes) -> str:
    import numpy as np
    from PIL import Image

    img = np.array(Image.open(io.BytesIO(content)).convert("RGB"))
    return _ocr_ndarray(img)


def ocr_pdf(content: bytes) -> str:
    import fitz
    import numpy as np
    from PIL import Image

    doc = fitz.open(stream=content, filetype="pdf")
    lines = []
    for page in doc:
        pix = page.get_pixmap(dpi=200)
        img = np.array(Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB"))
        lines.append(_ocr_ndarray(img))
    return "\n".join(s for s in lines if s)


# ---------- 路由 ----------


def parse_file(name: str, content: bytes) -> Tuple[str, bool]:
    """按扩展名路由；返回 (text, is_scanned)。is_scanned=True 表示需走 OCR。"""
    ext = _ext(name)
    if ext == ".pdf":
        return extract_pdf(content)
    if ext in (".doc", ".docx"):
        return extract_docx(content), False
    if ext in (".txt", ".md"):
        return extract_txt(content), False
    if ext in (".png", ".jpg", ".jpeg"):
        return "", True
    raise ValueError(f"不支持的文件类型：{ext}")


def ocr_by_name(name: str, content: bytes) -> str:
    return ocr_pdf(content) if _ext(name) == ".pdf" else ocr_image(content)
