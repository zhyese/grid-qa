"""Query 改写策略分类：判类型选 prompt+few-shot，正常 query 跳过（兼 adaptive 总开关）。

类型：
- colloquial（口语化，短或含口语词）→ 规范化改写
- abbreviation（含电网缩写 CT/PT/SF6 等）→ 展开改写
- term_missing（含 term_service 非标准别名）→ 标准化改写
- normal（规范 query）→ skip=True，跳过整个改写流程（adaptive）
"""
import json
from functools import lru_cache
from pathlib import Path

# 口语词集（命中即判 colloquial）
_COLLOQUIAL = {"咋", "咋办", "咋整", "啥", "啥叫", "嘛", "啥样", "咋样", "咋回事", "咋弄"}
# 电网常见缩写（命中即判 abbreviation）
_ABBR = {"CT", "PT", "SF6", "GIS", "VT", "AVR", "RTU", "SCADA", "UPS", "SVG", "FACTS"}


@lru_cache
def _load_fewshot() -> dict:
    p = Path(__file__).resolve().parent.parent / "data" / "rewrite_fewshot.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def get_fewshot(type_: str) -> list[dict]:
    """取该类型的 few-shot 示例列表。"""
    return _load_fewshot().get(type_, [])


def classify(query: str) -> dict:
    """判 query 类型。返回 {"type", "skip", "hint"}。normal → skip=True。"""
    if not query or not query.strip():
        return {"type": "normal", "skip": True, "hint": "空 query"}
    q = query.strip()
    # 口语化：短 或 含口语词
    if len(q) < 8 or any(w in q for w in _COLLOQUIAL):
        return {"type": "colloquial", "skip": False, "hint": "口语化，需规范化"}
    # 缩写：含电网缩写词（大小写不敏感）
    upper = q.upper()
    if any(a in upper for a in _ABBR):
        return {"type": "abbreviation", "skip": False, "hint": "含缩写，需展开为全称"}
    # 术语缺失：含 term_service 的非标准别名
    try:
        from app.services.term_service import _load_terms
        aliases = set(_load_terms().keys())
        if any(a and a in q for a in aliases):
            return {"type": "term_missing", "skip": False, "hint": "含非标准术语别名，需标准化"}
    except Exception:
        pass
    return {"type": "normal", "skip": True, "hint": "规范 query，无需改写"}
