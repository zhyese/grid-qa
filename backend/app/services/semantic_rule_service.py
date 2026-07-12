"""语义增强规则自定义（BRD §4.1.3）。

管理员定义「维度→关键词→标签」规则，apply 到文档/分块文本，给内容打上结构化语义维度
（如 作业场景=倒闸操作 / 安全等级=高风险）。规则存 data/semantic_rules.json。

可在文档解析时调用 enrich 给 chunk 打元数据维度，也可作为独立标签服务。
"""
import json
from functools import lru_cache
from pathlib import Path


def _path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "semantic_rules.json"


@lru_cache
def _load() -> list[dict]:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save(rules: list[dict]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
    _load.cache_clear()


def list_rules() -> list[dict]:
    return _load()


def add_rule(dimension: str, tag: str, keywords: list[str]) -> dict:
    dimension, tag = (dimension or "").strip(), (tag or "").strip()
    kws = [k.strip() for k in (keywords or []) if k and k.strip()]
    if not dimension or not tag or not kws:
        from app.core.response import BizError
        raise BizError("维度/标签/关键词都不能为空", 400)
    rules = _load()
    rule = {"dimension": dimension, "tag": tag, "keywords": kws}
    rules.append(rule)
    _save(rules)
    return rule


def delete_rule(idx: int) -> dict:
    rules = _load()
    if 0 <= idx < len(rules):
        rules.pop(idx)
        _save(rules)
    return {"idx": idx, "deleted": True}


def apply_rules(text: str) -> dict:
    """对文本应用全部规则 → {dimension: [tag,...]}（一维可多标签）。空文本返回 {}。"""
    if not text:
        return {}
    out: dict[str, list[str]] = {}
    for r in _load():
        if any(k in text for k in r.get("keywords", [])):
            out.setdefault(r["dimension"], []).append(r["tag"])
    return out
