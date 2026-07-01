"""两票智能审核（D3 审核）：规则引擎 + LLM 双层。

补齐"生成 → 审核"闭环：parse_ticket 启发式结构化 → _rule_check 确定性硬规则
（可配 ticket_rules.json，缺失回落 _DEFAULT_RULES）+ _llm_check 语义合规（复用
get_llm_provider, temperature=0）→ _aggregate 聚合打分。LLM 失败走 degraded()
降级只返回规则层；规则层始终可用。
"""
import json
import re
import time
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


def _item(layer: str, rule_id: str, typ: str, severity: str, msg: str, suggestion: str = "") -> dict:
    return {"layer": layer, "ruleId": rule_id, "type": typ,
            "severity": severity, "msg": msg, "suggestion": suggestion}


def _find_idx(steps: list[str], kw: str):
    for i, s in enumerate(steps):
        if kw in s:
            return i
    return None


def _rule_check(parsed: dict, rules: dict) -> list[dict]:
    """确定性硬规则：必填项 / 操作顺序 / 危险点 / 调度格式 / 禁用术语。"""
    items: list[dict] = []
    # 1) required_field
    for r in rules.get("required_fields", []):
        key, label = r.get("field"), r.get("label", r.get("field"))
        if key and not parsed.get(key):
            items.append(_item("rule", r["id"], "required_field", r["severity"],
                               f"缺少{label}", r.get("suggestion", "")))
    # 2) sequence：before 出现在 after 之前 → 违安措
    steps = parsed.get("steps", [])
    for r in rules.get("sequences", []):
        ib, ia = _find_idx(steps, r["before"]), _find_idx(steps, r["after"])
        if ib is not None and ia is not None and ib < ia:
            items.append(_item("rule", r["id"], "sequence", r["severity"],
                               r["msg"], r.get("suggestion", "")))
    # 3) danger_point：含高危关键词但未列危险点
    blob = parsed.get("raw", "") + "".join(steps)
    if any(k in blob for k in rules.get("danger_keywords", [])) and not parsed.get("dangers"):
        items.append(_item("rule", "DANGER_001", "danger_point", "major",
                           "涉及高风险操作但未列出危险点", "补充对应危险点分析"))
    # 4) dispatch_format
    pat = rules.get("dispatch_pattern")
    if pat and parsed.get("dispatch_no") and not re.match(pat, parsed["dispatch_no"]):
        items.append(_item("rule", "DISP_001", "dispatch_format", "minor",
                           "调度指令号格式不规范", "按 字母数字- 的规范格式填写"))
    # 5) keyword_blocklist
    for kw in rules.get("blocklist", []):
        if kw in blob:
            items.append(_item("rule", "BLOCK_001", "keyword_blocklist", "major",
                               f"含禁用表述：{kw}", "按安规规范表述"))
    return items


_LLM_AUDIT_PROMPT = """你是电网两票审核专家。规则引擎已查硬性项（必填/顺序/危险点/调度格式/禁用术语），
你专查语义合规：
1. 安措是否覆盖所有危险点与高风险操作
2. 术语是否规范、步骤是否可执行、有无遗漏关键步骤
输出严格 JSON 数组，每项 {{"ruleId":"LLM_xxx","severity":"critical|major|minor","msg":"问题描述","suggestion":"修正建议"}}；无问题输出 []。

【票据类型】{ttype}
【任务】{task}
【操作步骤】{steps}
【安全措施】{safety}
【危险点】{dangers}"""


def _extract_json(ans: str):
    """从 LLM 回答里抠 JSON（数组或对象）；镜像 domain_service._extract_json。"""
    m = re.search(r"(\{.*\}|\[.*\])", ans or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


async def _llm_check(parsed: dict, ticket_type: str, model_type: str | None) -> list[dict]:
    """LLM 语义审核（temperature=0，结构化 JSON）。"""
    provider = get_llm_provider(model_type)
    prompt = _LLM_AUDIT_PROMPT.format(
        ttype=ticket_type, task=parsed.get("task", ""),
        steps=";".join(parsed.get("steps", [])) or "无",
        safety=";".join(parsed.get("safety", [])) or "无",
        dangers=";".join(parsed.get("dangers", [])) or "无",
    )
    ans = await provider.chat([{"role": "user", "content": prompt}], temperature=0, max_tokens=800)
    arr = _extract_json(ans)
    items: list[dict] = []
    if isinstance(arr, list):
        for it in arr:
            if not isinstance(it, dict):
                continue
            items.append(_item("llm", it.get("ruleId", "LLM_xxx"), "semantic",
                               it.get("severity", "minor"), it.get("msg", ""), it.get("suggestion", "")))
    return items


_DEDUCT = {"critical": 35, "major": 15, "minor": 5}


def _score(items: list[dict]) -> int:
    s = 100 - sum(_DEDUCT.get(it.get("severity", "minor"), 5) for it in items)
    return max(0, min(100, s))


def _overall(score: int) -> str:
    return "pass" if score >= 85 else "warn" if score >= 60 else "fail"


def _inc_metric(overall: str) -> None:
    try:
        from app.core import metrics
        metrics.TICKET_AUDIT.labels(overall).inc()
    except Exception:
        pass


async def audit_ticket(text: str, ticket_type: str = "操作票", model_type: str | None = None) -> dict:
    """编排双层审核 + 聚合。LLM 失败降级只返回规则层。"""
    t0 = time.perf_counter()
    rules = _load_rules()
    parsed = parse_ticket(text, ticket_type)
    latency = lambda: int((time.perf_counter() - t0) * 1000)

    # 解析为空短路
    if not parsed["task"] and not parsed["steps"]:
        report = {"overall": "fail", "score": 0, "ticketType": ticket_type,
                  "items": [_item("rule", "PARSE_001", "parse", "critical",
                                  "无法识别票据内容", "请按规范格式粘贴票据（含任务/步骤等）")],
                  "latencyMs": latency()}
        _inc_metric("fail")
        return report

    rule_items = _rule_check(parsed, rules)
    try:
        llm_items = await _llm_check(parsed, ticket_type, model_type)
    except Exception as e:
        degraded("ticket_audit_llm", e)
        llm_items = []

    items = rule_items + llm_items
    score = _score(items)
    overall = _overall(score)
    _inc_metric(overall)
    return {"overall": overall, "score": score, "ticketType": ticket_type,
            "items": items, "latencyMs": latency()}
