# backend/app/schemas/citation.py
"""第三层 · 标准化引用输出 schema + 降级解析。"""
import json
import re
from typing import Optional

from pydantic import BaseModel, Field

from app.rag.citation import extract_sentence_sources, split_sentences


class CitationItem(BaseModel):
    sentence: str
    ref_id: int
    chunk_id: str = ""
    metadata: dict = Field(default_factory=dict)  # doc_title/section_path/page_num/original_text


class CitationAnswer(BaseModel):
    answer_text: str
    citation_map: list[CitationItem] = Field(default_factory=list)
    unverified_claim: list[str] = Field(default_factory=list)
    structured: bool = True  # True=LLM 直出 JSON；False=纯文本降级反查


_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_citation_answer(raw: str, index: dict[int, str], contexts: list[dict] | None = None) -> CitationAnswer:
    """解析 LLM 输出为 CitationAnswer。

    优先：LLM 直出 JSON → 结构化。
    降级：纯文本 → answer_text=原文，citation_map 用 evidence_trace 反查 [n]→index→chunk_id。
    两条路径都不抛（失败返回仅含 answer_text 的空壳）。
    """
    if not raw:
        return CitationAnswer(answer_text="", structured=False)
    m = _JSON_RE.search(raw)
    if m:
        try:
            d = json.loads(m.group(0))
            if "answer_text" in d:
                return CitationAnswer(**d)
        except Exception:
            pass
    # 降级：纯文本 + evidence_trace 反查
    ctx_meta = {c.get("chunkId"): c for c in (contexts or [])}
    cmap: list[CitationItem] = []
    for s in split_sentences(raw):
        for ref in extract_sentence_sources(s):
            cid = index.get(ref, "")
            meta = ctx_meta.get(cid, {})
            cmap.append(CitationItem(
                sentence=s, ref_id=ref, chunk_id=cid,
                metadata={"doc_title": meta.get("docName", ""), "section_path": "",
                          "page_num": meta.get("page_num"), "original_text": meta.get("chunk", "")},
            ))
    return CitationAnswer(answer_text=raw, citation_map=cmap, structured=False)
