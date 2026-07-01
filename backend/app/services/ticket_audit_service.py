"""两票智能审核（D3 审核）：规则引擎 + LLM 双层。

补齐"生成 → 审核"闭环：parse_ticket 启发式结构化 → _rule_check 确定性硬规则
（可配 ticket_rules.json，缺失回落 _DEFAULT_RULES）+ _llm_check 语义合规（复用
get_llm_provider, temperature=0）→ _aggregate 聚合打分。LLM 失败走 degraded()
降级只返回规则层；规则层始终可用。
"""
import json
import re
from pathlib import Path

from app.core.obs import degraded
from app.providers.factory import get_llm_provider

_RULES_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "ticket_rules.json"

# 单行字段（任务 / 调度指令号 / 操作人|负责人）—— 顺序无关，re.search 全文找
_FIELD_RE = [
    ("task", r"(?:操作任务|工作任务|任务)\s*[:：]\s*(.+)"),
    ("dispatch_no", r"(?:调度)?(?:指令|命令)(?:号|编号)?\s*[:：]\s*(.+)"),
    ("operator", r"(?:操作人|工作负责人|负责人|监护人)\s*[:：]\s*(.+)"),
]

_STEP_MARKERS = ("操作步骤", "步骤", "操作内容")
_SAFETY_MARKERS = ("安全措施", "安措", "安全技术措施")
_DANGER_MARKERS = ("危险点", "风险点")


def _strip_num(line: str) -> str:
    """去掉行首序号：'1.' '1、' '1)' '①' '- ' '* '。"""
    return re.sub(r"^\s*(?:\d+[.、)）]\s*|[①-⑳]\s*|[-*•]\s*)", "", line).strip()


def _is_header(line: str, markers: tuple[str, ...]) -> bool:
    s = _strip_num(line.rstrip(":： ").strip())
    return any(m in s for m in markers) and len(s) <= 14


def parse_ticket(text: str, ticket_type: str = "操作票") -> dict:
    """启发式结构化解析：单行字段正则 + 分段扫描 steps/safety/dangers。"""
    raw = text or ""
    parsed = {"ticket_type": ticket_type, "task": "", "dispatch_no": "",
              "operator": "", "steps": [], "safety": [], "dangers": [], "raw": raw}
    if not raw.strip():
        return parsed
    # 1) 单行字段
    for key, pat in _FIELD_RE:
        m = re.search(pat, raw, re.M)
        if m:
            parsed[key] = m.group(1).strip()
    # 2) 分段扫描
    section = None
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if _is_header(s, _STEP_MARKERS):
            section = "steps"; continue
        if _is_header(s, _SAFETY_MARKERS):
            section = "safety"; continue
        if _is_header(s, _DANGER_MARKERS):
            section = "dangers"; continue
        # 跳过已被字段正则消费的行
        if re.match(r"(?:操作任务|工作任务|任务)\s*[:：]", s) \
           or re.match(r"(?:调度)?(?:指令|命令)", s) \
           or re.match(r"(?:操作人|工作负责人|负责人|监护人)\s*[:：]", s):
            continue
        item = _strip_num(s)
        if not item:
            continue
        if section == "steps":
            parsed["steps"].append(item)
        elif section == "safety":
            parsed["safety"].append(item)
        elif section == "dangers":
            parsed["dangers"].append(item)
        elif re.match(r"\d+[.、)]", s) or re.match(r"[①-⑳]", s):
            parsed["steps"].append(item)   # 无标题时，编号行默认归步骤
    return parsed


_DEFAULT_RULES = {
    "required_fields": [
        {"id": "REQ_TASK", "field": "task", "label": "任务", "severity": "critical", "suggestion": "补充操作任务"},
        {"id": "REQ_DISPATCH", "field": "dispatch_no", "label": "调度指令号", "severity": "major", "suggestion": "补充调度指令号"},
        {"id": "REQ_OPERATOR", "field": "operator", "label": "操作人", "severity": "major", "suggestion": "补充操作人/负责人"},
    ],
    "sequences": [
        {"id": "SEQ_001", "before": "挂地线", "after": "验电", "severity": "critical",
         "msg": "挂地线出现在验电之前", "suggestion": "应先验电确认无电后再挂地线"},
        {"id": "SEQ_002", "before": "接地", "after": "验电", "severity": "critical",
         "msg": "接地出现在验电之前", "suggestion": "应先验电确认无电后再接地"},
    ],
    "danger_keywords": ["停电", "接地", "倒闸", "拉闸", "合闸", "带电", "登高", "挂地线"],
    "dispatch_pattern": r"^[A-Za-z0-9\-]{4,}$",
    "blocklist": ["约时停送电", "约定停送电", "口头指令"],
}


def _load_rules() -> dict:
    """读 ticket_rules.json；缺失/非法回落 _DEFAULT_RULES（规则层始终可用）。"""
    try:
        if _RULES_PATH.exists():
            data = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        degraded("ticket_rules_load", e)
    return _DEFAULT_RULES
