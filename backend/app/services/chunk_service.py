"""文本分块：段落累积 + 长段按句末边界切，保留语义完整性。

结构感知分块（S5/A4 升级）：
- 表格段整体成块（chunk_type=table），不被字符切两半
- 正文段先切父块（大窗口 PARENT_SIZE），父块内再切子块（CHUNK_SIZE）
- 子块记录 parent_idx：检索子块（精度），召回同组父块全文给 LLM（完整上下文）
"""
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


# ---------- 结构感知分块 + Parent-Child（small-to-big）----------

# 标题启发式：编号开头或含规程关键词的短行视为章节标题
_SECTION_PREFIXES = ("第", "一、", "二、", "三、", "1.", "2.", "1、", "2、")
_SECTION_KEYWORDS = ("规程", "要求", "步骤", "措施", "注意", "巡视", "验收", "周期")


def _detect_section(text: str) -> str:
    """启发式取首行作为章节标题（短行、常见标题特征）。失败返回空串。"""
    first = (text or "").lstrip().split("\n", 1)[0].strip()
    if not first or len(first) > 40:
        return ""
    if any(first.startswith(p) for p in _SECTION_PREFIXES):
        return first
    if any(k in first for k in _SECTION_KEYWORDS):
        return first
    return ""


def split_structured(
    sections: list[dict],
    parent_size: int | None = None,
    child_size: int | None = None,
    overlap: int | None = None,
) -> list[dict]:
    """结构感知分块：表格整体成块；正文父→子两层切，子块带 parent_idx。

    输入 sections: [{type:"text"|"table", content, page_num?, bbox?, table_header?}]
    输出 chunks: [{text, chunk_type, section, parent_idx, section_path,
                   page_num, bbox, table_header}]（顺序即 chunk_idx）
      - 表格：自身即一个父组（parent_idx=自身组号），chunk_type=table，带 table_header
      - 正文：先切父块(大窗口)，每父块内切子块(小窗口)，同父块子块共享 parent_idx
      - 引用元数据透传：section 级 page_num/bbox/table_header 落到每个产出 chunk；
        section_path 用 _detect_section 启发式标题（无标题则空串，由 parse_documents 兜底用 section）
    检索用子块（入向量库/BM25），命中后按 parent_idx 聚合同组拼父块全文给 LLM。
    """
    parent_size = parent_size or settings.PARENT_SIZE
    child_size = child_size or settings.CHUNK_SIZE
    overlap = overlap if overlap is not None else settings.CHUNK_OVERLAP

    chunks: list[dict] = []
    group_id = 0  # 父块组号
    for sec in sections or []:
        stype, content = sec.get("type", "text"), sec.get("content", "") or ""
        page_num = sec.get("page_num")
        bbox = sec.get("bbox")
        table_header = sec.get("table_header", "")
        if stype == "table":
            md = content.strip()
            if not md:
                continue
            chunks.append({"text": md, "chunk_type": "table",
                           "section": "表格", "section_path": "表格",
                           "parent_idx": group_id,
                           "page_num": page_num, "bbox": bbox, "table_header": table_header})
            group_id += 1
            continue
        # 正文：先父块（大窗口，按段落/句末），再子块（小窗口）
        if not content.strip():
            continue
        big_blocks = split_text(content, chunk_size=parent_size, overlap=settings.PARENT_OVERLAP)
        for big in big_blocks:
            gid = group_id
            group_id += 1
            section_title = _detect_section(big)
            smalls = split_text(big, chunk_size=child_size, overlap=overlap)
            # 父块过短时 smalls 只有一块，仍是子块（parent_idx 指向自身组）
            for s in smalls:
                if s.strip():
                    chunks.append({"text": s, "chunk_type": "child",
                                   "section": section_title, "section_path": section_title,
                                   "parent_idx": gid,
                                   "page_num": page_num, "bbox": bbox, "table_header": ""})
    return chunks
