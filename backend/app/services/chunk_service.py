"""文本分块：递归字符切分 + overlap + 术语归一化。"""
from app.config import settings
from app.services.term_service import normalize


def split_text(
    text: str,
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    """按字符滑窗切分，中文友好。返回归一化后的非空块列表。"""
    chunk_size = chunk_size or settings.CHUNK_SIZE
    overlap = overlap if overlap is not None else settings.CHUNK_OVERLAP
    text = normalize((text or "").strip())
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    step = max(1, chunk_size - overlap)
    chunks = []
    for start in range(0, len(text), step):
        chunks.append(text[start : start + chunk_size])
        if start + chunk_size >= len(text):
            break
    return chunks
