"""文本分块：段落累积 + 长段按句末边界切，保留语义完整性。"""
from app.config import settings
from app.services.term_service import normalize

# 句末边界符（中文标点优先）
_SENTENCE_ENDS = ("。", "！", "？", "；", "\n", "；", ".", "!", "?", ";")


def _split_long(text: str, chunk_size: int, overlap: int) -> list[str]:
    """超长段落按字符切，尽量在 chunk_size 附近的句末断开，避免切半句。"""
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            # 在 [start+half, end] 区间找最近的句末符
            half = start + chunk_size // 2
            best = -1
            for sb in _SENTENCE_ENDS:
                pos = text.rfind(sb, half, end)
                if pos > best:
                    best = pos
            if best > half:
                end = best + 1  # 含句末符
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)  # overlap 但保证前进
    return chunks


def split_text(
    text: str,
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    """先按行/段落累积到 chunk_size；单段超长则按句末边界切。返回归一化后的非空块。"""
    chunk_size = chunk_size or settings.CHUNK_SIZE
    overlap = overlap if overlap is not None else settings.CHUNK_OVERLAP
    text = normalize((text or "").strip())
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paragraphs:
        if len(p) > chunk_size:
            # 先落盘当前累积，再切长段
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.extend(_split_long(p, chunk_size, overlap))
        elif len(cur) + len(p) + 1 <= chunk_size:
            cur = f"{cur}\n{p}" if cur else p
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks
