"""电网术语归一化：别名/错别字 → 标准术语。词表见 data/grid_terms.json。"""
import json
from functools import lru_cache
from pathlib import Path


@lru_cache
def _load_terms() -> dict:
    p = Path(__file__).resolve().parent.parent / "data" / "grid_terms.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def normalize(text: str) -> str:
    """逐词替换归一化。用占位符保护已有标准词，避免 '主变' 误伤 '主变压器'。"""
    if not text:
        return text
    terms = _load_terms()
    std_words = set(terms.values())
    # 1) 先把已存在的标准词替换为占位符，避免被别名误伤
    prot = {}
    for i, w in enumerate(sorted(std_words, key=len, reverse=True)):
        ph = f"\x00S{i}\x00"
        if w in text:
            prot[ph] = w
            text = text.replace(w, ph)
    # 2) 替换别名 → 标准词
    for alias in sorted(terms, key=len, reverse=True):
        if alias in text:
            text = text.replace(alias, terms[alias])
    # 3) 恢复标准词
    for ph, w in prot.items():
        text = text.replace(ph, w)
    return text
