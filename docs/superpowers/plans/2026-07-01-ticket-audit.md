# 两票智能审核（ticket-audit）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐"两票生成 → 两票审核"闭环：输入已填的操作票/工作票文本，输出规则引擎 + LLM 双层结构化审核报告（overall/score/items）。

**Architecture:** 后端新增 `ticket_audit_service`：`parse_ticket` 启发式结构化解析 → `_rule_check`（确定性硬规则，可配 JSON）+ `_llm_check`（语义合规，复用 `get_llm_provider` temperature=0）→ `_aggregate` 聚合打分。LLM 失败走 `degraded()` 降级只返回规则层。路由 `POST /domain/ticket/audit`（admin + 限流 + 操作日志），前端 `Diagnose.vue` 加第 4 个 tab。

**Tech Stack:** FastAPI / Pydantic v2 / Vue 3 / prometheus_client / pytest（异步用 `asyncio.run`，无 pytest-asyncio 依赖）

## Global Constraints

- **规则配置格式用 JSON 不是 YAML**：项目无 PyYAML 依赖（见 `backend/requirements.txt`），新增依赖在 Win+代理环境是摩擦点。规则文件 `backend/data/ticket_rules.json`（stdlib `json` 零依赖），缺失/非法时回落 `_DEFAULT_RULES`。**这是对 spec 的明确偏离**（spec 写 `.yaml`），理由=避免新依赖；韧性设计不变。
- 测试落点 `tests/test_ticket_audit.py`（顶层 `tests/`，与既有结构一致；非 spec 写的 `backend/tests/`）
- 后端无 pytest-asyncio：异步函数测试用同步测试函数包 `asyncio.run(...)`（项目既有测试全是同步的）
- 后端运行：`venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --app-dir backend`（无 --reload，改完重启）
- 测试运行：`venv/Scripts/python.exe -m pytest tests/test_ticket_audit.py -v`（conftest 已把 `backend` 加入 sys.path）
- 复用既有模式：`degraded(tag, e)` 降级、`get_llm_provider(mt).chat(msgs, temperature=0)`、`success(data, msg)` 统一响应、`@limiter.limit` + `Depends(require_admin)` + `write_log`
- 打分模型（确定性）：每项按严重度扣分 `critical=35 / major=15 / minor=5`，`score = max(0, 100 - Σ扣分)`；`overall = pass(score≥85) / warn(60–84) / fail(<60)`

## File Structure

- **Create:** `backend/app/services/ticket_audit_service.py` — 核心：`parse_ticket` / `_load_rules` / `_DEFAULT_RULES` / `_rule_check` / `_llm_check` / `_aggregate` / `audit_ticket`
- **Create:** `backend/data/ticket_rules.json` — 可配规则（JSON），内容 = `_DEFAULT_RULES` 的可编辑副本
- **Create:** `backend/data/golden_tickets.json` — 10 例标注票据回归集
- **Create:** `tests/test_ticket_audit.py` — 单测 + 回归门禁
- **Modify:** `backend/app/schemas/domain.py` — 加 `TicketAuditRequest`
- **Modify:** `backend/app/routers/domain.py` — 加 `POST /domain/ticket/audit`（admin）
- **Modify:** `backend/app/core/metrics.py` — 加 `TICKET_AUDIT` Counter + `init_metric_series` 预注册
- **Modify:** `frontend/src/api/index.js` — 加 `auditTicket`
- **Modify:** `frontend/src/views/Diagnose.vue` — 第 4 个 tab「两票审核」+ 结果渲染

---

### Task 1: 结构化解析 + 规则配置层（parse_ticket / _load_rules / _DEFAULT_RULES）

**Files:**
- Create: `backend/app/services/ticket_audit_service.py`
- Create: `backend/data/ticket_rules.json`
- Test: `tests/test_ticket_audit.py`

**Interfaces:**
- Produces:
  - `parse_ticket(text: str, ticket_type: str = "操作票") -> dict`，返回结构：
    ```python
    {"ticket_type": str, "task": str, "dispatch_no": str, "operator": str,
     "steps": list[str], "safety": list[str], "dangers": list[str], "raw": str}
    ```
  - `_load_rules() -> dict`（读 JSON，失败回落 `_DEFAULT_RULES`）
  - `_DEFAULT_RULES: dict`（硬编码兜底规则，结构见 Step 3）

- [ ] **Step 1: 写失败测试（parse_ticket + _load_rules）**

`tests/test_ticket_audit.py`：
```python
"""两票智能审核单测：解析 / 规则引擎 / LLM语义 / 聚合 / 端到端。"""
import asyncio
from app.services import ticket_audit_service as svc

_OP_TICKET = """操作任务：1号主变由运行转检修
调度指令号：DD-2026-001
操作人：张三
操作步骤：
1. 断开1号主变10kV侧断路器
2. 验电
3. 挂地线
安全措施：
- 戴绝缘手套
危险点：
- 触电
"""

_WORK_TICKET = """工作任务：2号线路检修
调度指令号：DD-2026-002
工作负责人：李四
操作步骤：
1. 停电
2. 验电
3. 接地
危险点：
- 高处坠落
"""


def test_parse_ticket_op_fields():
    p = svc.parse_ticket(_OP_TICKET, "操作票")
    assert p["ticket_type"] == "操作票"
    assert "1号主变" in p["task"]
    assert p["dispatch_no"] == "DD-2026-001"
    assert p["operator"] == "张三"
    assert [s for s in p["steps"] if "验电" in s], "应解析出验电步骤"
    assert [s for s in p["steps"] if "挂地线" in s], "应解析出挂地线步骤"
    assert any("绝缘手套" in s for s in p["safety"])
    assert any("触电" in d for d in p["dangers"])


def test_parse_ticket_work_fields():
    p = svc.parse_ticket(_WORK_TICKET, "工作票")
    assert "2号线路" in p["task"]
    assert p["operator"] == "李四"           # 工作负责人 → operator
    assert p["ticket_type"] == "工作票"


def test_parse_ticket_empty():
    p = svc.parse_ticket("", "操作票")
    assert p["task"] == "" and p["steps"] == [] and p["dangers"] == []


def test_load_rules_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "_RULES_PATH", tmp_path / "nope.json")
    r = svc._load_rules()
    assert "required_fields" in r and r.get("sequences"), "缺失文件应回落 _DEFAULT_RULES"


def test_load_rules_reads_json(tmp_path, monkeypatch):
    f = tmp_path / "ticket_rules.json"
    f.write_text(
        '{"required_fields":[],"sequences":[{"id":"X","before":"a","after":"b",'
        '"severity":"minor","msg":"m","suggestion":"s"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(svc, "_RULES_PATH", f)
    assert svc._load_rules()["sequences"][0]["id"] == "X"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_ticket_audit.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.services.ticket_audit_service'`）

- [ ] **Step 3: 实现 service 骨架（解析 + 配置）**

`backend/app/services/ticket_audit_service.py`：
```python
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
```

`backend/data/ticket_rules.json`（= `_DEFAULT_RULES` 的可编辑副本）：
```json
{
  "required_fields": [
    {"id": "REQ_TASK", "field": "task", "label": "任务", "severity": "critical", "suggestion": "补充操作任务"},
    {"id": "REQ_DISPATCH", "field": "dispatch_no", "label": "调度指令号", "severity": "major", "suggestion": "补充调度指令号"},
    {"id": "REQ_OPERATOR", "field": "operator", "label": "操作人", "severity": "major", "suggestion": "补充操作人/负责人"}
  ],
  "sequences": [
    {"id": "SEQ_001", "before": "挂地线", "after": "验电", "severity": "critical", "msg": "挂地线出现在验电之前", "suggestion": "应先验电确认无电后再挂地线"},
    {"id": "SEQ_002", "before": "接地", "after": "验电", "severity": "critical", "msg": "接地出现在验电之前", "suggestion": "应先验电确认无电后再接地"}
  ],
  "danger_keywords": ["停电", "接地", "倒闸", "拉闸", "合闸", "带电", "登高", "挂地线"],
  "dispatch_pattern": "^[A-Za-z0-9\\-]{4,}$",
  "blocklist": ["约时停送电", "约定停送电", "口头指令"]
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_ticket_audit.py -v`
Expected: PASS（5 个测试全过）

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/ticket_audit_service.py backend/data/ticket_rules.json tests/test_ticket_audit.py
git commit -m "feat(ticket-audit): 结构化解析 + 规则配置层（parse_ticket/_load_rules）"
```

---

### Task 2: 规则引擎 _rule_check（5 类硬规则）

**Files:**
- Modify: `backend/app/services/ticket_audit_service.py`（追加 `_item` / `_find_idx` / `_rule_check`）
- Test: `tests/test_ticket_audit.py`（追加规则单测）

**Interfaces:**
- Consumes: Task 1 的 `parse_ticket` 输出 dict + `_load_rules()` / `_DEFAULT_RULES` 结构
- Produces: `_rule_check(parsed: dict, rules: dict) -> list[dict]`，每个 item：
  ```python
  {"layer": "rule", "ruleId": str, "type": str, "severity": "critical|major|minor", "msg": str, "suggestion": str}
  ```
  ruleId 约定：`REQ_*`(必填) / `SEQ_*`(顺序) / `DANGER_001`(危险点) / `DISP_001`(调度格式) / `BLOCK_001`(禁用术语)

- [ ] **Step 1: 写失败测试（每类规则正反例）**

追加到 `tests/test_ticket_audit.py`：
```python
def _parsed(**over):
    base = {"task": "t", "dispatch_no": "DD-2026-001", "operator": "x",
            "steps": [], "safety": [], "dangers": ["d"], "raw": ""}
    base.update(over)
    return base


def test_rule_required_field_present_then_absent():
    p = _parsed(operator="张三")
    assert not any(it["ruleId"] == "REQ_OPERATOR" for it in svc._rule_check(p, svc._DEFAULT_RULES))
    p2 = _parsed(operator="")
    assert any(it["ruleId"] == "REQ_OPERATOR" for it in svc._rule_check(p2, svc._DEFAULT_RULES))


def test_rule_sequence_violation_and_correct_order():
    bad = _parsed(steps=["挂地线", "验电"])           # 挂地线在验电前 → 违安措
    assert "SEQ_001" in [it["ruleId"] for it in svc._rule_check(bad, svc._DEFAULT_RULES)]
    good = _parsed(steps=["验电", "挂地线"])          # 正确顺序不命中
    assert "SEQ_001" not in [it["ruleId"] for it in svc._rule_check(good, svc._DEFAULT_RULES)]


def test_rule_danger_point_missing_when_high_risk():
    p = _parsed(steps=["停电操作"], raw="涉及停电", dangers=[])
    assert any(it["ruleId"] == "DANGER_001" for it in svc._rule_check(p, svc._DEFAULT_RULES))


def test_rule_danger_point_ok_when_listed():
    p = _parsed(steps=["停电操作"], raw="涉及停电", dangers=["触电"])
    assert not any(it["ruleId"] == "DANGER_001" for it in svc._rule_check(p, svc._DEFAULT_RULES))


def test_rule_dispatch_format():
    p = _parsed(dispatch_no="非法")
    assert any(it["ruleId"] == "DISP_001" for it in svc._rule_check(p, svc._DEFAULT_RULES))


def test_rule_blocklist():
    p = _parsed(steps=["约时停送电送电"], raw="约时停送电", dangers=["d"])
    assert any(it["ruleId"] == "BLOCK_001" for it in svc._rule_check(p, svc._DEFAULT_RULES))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_ticket_audit.py -v`
Expected: FAIL（`AttributeError: module ... has no attribute '_rule_check'`）

- [ ] **Step 3: 实现 _rule_check**

追加到 `ticket_audit_service.py`：
```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_ticket_audit.py -v`
Expected: PASS（11 个测试全过）

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/ticket_audit_service.py tests/test_ticket_audit.py
git commit -m "feat(ticket-audit): 规则引擎 _rule_check（必填/顺序/危险点/调度格式/禁用术语）"
```

---

### Task 3: LLM 语义审核 + 聚合打分 + 编排 audit_ticket

**Files:**
- Modify: `backend/app/services/ticket_audit_service.py`（追加 `_extract_json` / `_llm_check` / `_score` / `_overall` / `audit_ticket`）
- Test: `tests/test_ticket_audit.py`（追加 LLM mock + 聚合 + 端到端单测）

**Interfaces:**
- Consumes: Task 1/2 的 `parse_ticket` / `_rule_check` / `_load_rules`
- Produces:
  - `async audit_ticket(text: str, ticket_type: str = "操作票", model_type: str | None = None) -> dict`
    ```python
    {"overall": "pass"|"warn"|"fail", "score": int, "ticketType": str,
     "items": list[dict], "latencyMs": int}
    ```
  - `async _llm_check(parsed, ticket_type, model_type) -> list[dict]`（item 的 `layer="llm"`、`type="semantic"`）
  - `_score(items) -> int` / `_overall(score) -> str`（纯函数）

- [ ] **Step 1: 写失败测试（mock LLM + 聚合 + 降级 + 端到端）**

追加到 `tests/test_ticket_audit.py`：
```python
class _FakeProvider:
    def __init__(self, resp): self.resp = resp
    async def chat(self, msgs, **kw): return self.resp


def test_scoring_pure():
    assert svc._score([]) == 100
    assert svc._score([{"severity": "critical"}]) == 65       # 100-35
    assert svc._score([{"severity": "critical"}, {"severity": "major"}]) == 50
    assert svc._score([{"severity": "critical"}, {"severity": "critical"}]) == 30
    assert svc._overall(90) == "pass"
    assert svc._overall(70) == "warn"
    assert svc._overall(50) == "fail"


def test_llm_check_parses_items(monkeypatch):
    monkeypatch.setattr(
        svc, "get_llm_provider",
        lambda mt=None: _FakeProvider('[{"ruleId":"LLM_1","severity":"major",'
                                      '"msg":"安措未覆盖危险点","suggestion":"补安措"}]'))
    items = asyncio.run(svc._llm_check(
        {"task": "t", "steps": ["停电"], "safety": [], "dangers": ["触电"]}, "操作票", None))
    assert len(items) == 1
    assert items[0]["layer"] == "llm" and items[0]["severity"] == "major"


def test_llm_check_empty_array(monkeypatch):
    monkeypatch.setattr(svc, "get_llm_provider", lambda mt=None: _FakeProvider("[]"))
    assert asyncio.run(svc._llm_check({"task": "t", "steps": [], "safety": [], "dangers": []}, "操作票", None)) == []


def test_audit_ticket_happy_path(monkeypatch):
    async def no_llm(*a, **k): return []
    monkeypatch.setattr(svc, "_llm_check", no_llm)
    report = asyncio.run(svc.audit_ticket(_OP_TICKET, "操作票", None))
    assert report["overall"] == "pass" and report["score"] == 100
    assert report["ticketType"] == "操作票" and "latencyMs" in report


def test_audit_ticket_empty_input():
    report = asyncio.run(svc.audit_ticket("", "操作票", None))
    assert report["overall"] == "fail" and report["score"] == 0
    assert report["items"][0]["severity"] == "critical"


def test_audit_ticket_degrades_on_llm_failure(monkeypatch):
    async def boom(*a, **k): raise RuntimeError("llm down")
    monkeypatch.setattr(svc, "_llm_check", boom)
    report = asyncio.run(svc.audit_ticket(_OP_TICKET, "操作票", None))
    assert report["overall"] in ("pass", "warn", "fail")
    assert report["items"], "降级应仍返回规则层结果"
    assert all(it["layer"] == "rule" for it in report["items"]), "LLM 失败不应有 llm 项"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_ticket_audit.py -v`
Expected: FAIL（`AttributeError: ... has no attribute '_score' / 'audit_ticket'`）

- [ ] **Step 3: 实现 _llm_check / 聚合 / audit_ticket**

追加到 `ticket_audit_service.py` 顶部 import 区：`import time`。再追加：
```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_ticket_audit.py -v`
Expected: PASS（17 个测试全过）

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/ticket_audit_service.py tests/test_ticket_audit.py
git commit -m "feat(ticket-audit): LLM 语义审核 + 聚合打分 + audit_ticket 编排（含降级）"
```

---

### Task 4: API 层（schema + 路由 + 指标）

**Files:**
- Modify: `backend/app/schemas/domain.py`（加 `TicketAuditRequest`）
- Modify: `backend/app/routers/domain.py`（加 `POST /domain/ticket/audit`）
- Modify: `backend/app/core/metrics.py`（加 `TICKET_AUDIT` + `init_metric_series` 预注册）

**Interfaces:**
- Consumes: Task 3 的 `audit_ticket(text, ticket_type, model_type)`
- Produces:
  - `POST /api/domain/ticket/audit`，body `TicketAuditRequest`，依赖 `require_admin`，限流 `10/minute`，写操作日志
  - 指标 `grid_ticket_audit_total{result="pass|warn|fail"}`，启动预注册 0 值

- [ ] **Step 1: 加 schema**

`backend/app/schemas/domain.py` 末尾追加：
```python
class TicketAuditRequest(BaseModel):
    ticketText: str                       # 已填票据全文（粘贴）
    ticketType: str = "操作票"             # 操作票 / 工作票
    modelType: Optional[str] = None
```

- [ ] **Step 2: 加路由（admin + 限流 + 日志）**

`backend/app/routers/domain.py`：
- import 行追加 `TicketAuditRequest`：
```python
from app.schemas.domain import DiagnoseRequest, SimilarCaseRequest, TicketAuditRequest, TicketRequest
```
- import 行追加 admin 依赖：
```python
from app.dependencies import get_current_user, require_admin
```
- import 区追加（与现有 `from app.services import domain_service` 并列）：
```python
from app.services import ticket_audit_service
```
- 文件末尾追加端点（审核逻辑在 `ticket_audit_service`，非 `domain_service`）：
```python
@router.post("/ticket/audit")
@limiter.limit("10/minute")
async def ticket_audit(
    request: Request,
    body: TicketAuditRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """两票智能审核：已填票据 → 规则引擎+LLM 双层审核报告（仅管理员）。"""
    data = await ticket_audit_service.audit_ticket(body.ticketText, body.ticketType, body.modelType)
    await write_log(db, user.username, "两票审核", f"{body.ticketType} | {body.ticketText[:40]}")
    return success(data, "审核完成")
```

- [ ] **Step 3: 加指标 + 预注册**

`backend/app/core/metrics.py`：
- 在 `DOMAIN_CALLS = Counter(...)` 之后追加：
```python
# 两票智能审核结果分布（pass/warn/fail）
TICKET_AUDIT = Counter("grid_ticket_audit_total", "两票智能审核结果", ["result"])
```
- 在 `init_metric_series()` 的 try 块内（`DOMAIN_CALLS.labels("safety_block").inc(0)` 之后）追加：
```python
        # 两票审核结果（预注册 0 值，消除面板 No data 盲区）
        for _res in ("pass", "warn", "fail"):
            TICKET_AUDIT.labels(_res).inc(0)
```

- [ ] **Step 4: 语法检查 + 重启 + curl 验证**

```bash
venv/Scripts/python.exe -m py_compile backend/app/schemas/domain.py backend/app/routers/domain.py backend/app/core/metrics.py backend/app/services/ticket_audit_service.py
# 重启后端，登录 admin 拿 token
TOK=$(curl -s -X POST http://127.0.0.1:8001/api/system/login -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}' | python -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
# 审核一张缺任务、顺序错的票（期望 fail）
curl -s -X POST http://127.0.0.1:8001/api/domain/ticket/audit \
  -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"ticketText":"调度指令号:DD-2026-001\n操作人:张三\n操作步骤:\n1. 挂地线\n2. 验电\n危险点:\n- 触电","ticketType":"操作票"}'
# 预期：code=200，data.overall=fail，data.items 含 SEQ_001(critical)
# 非管理员调用应 403（用普通用户 token 验证一条）
```

- [ ] **Step 5: Commit**
```bash
git add backend/app/schemas/domain.py backend/app/routers/domain.py backend/app/core/metrics.py
git commit -m "feat(ticket-audit): 审核端点 POST /domain/ticket/audit（admin+限流+日志+指标）"
```

---

### Task 5: golden 票据回归集 + 门禁测试

**Files:**
- Create: `backend/data/golden_tickets.json`
- Test: `tests/test_ticket_audit.py`（追加回归测试）

**Interfaces:**
- Consumes: Task 1–3 的 `audit_ticket`（LLM mock 为 `[]`，纯规则层确定性回归）
- Produces: 10 例标注票据，每例 `{"text","ticketType","expect":"pass|warn|fail"}`，CI 门禁

打分基线（critical=35/major=15/minor=5；pass≥85/warn 60–84/fail<60）下的期望：
- 0 项 → pass；1 critical → warn(65)；2+ critical → fail(≤30)

- [ ] **Step 1: 写 golden 数据 + 失败测试**

`backend/data/golden_tickets.json`：
```json
[
  {"text": "操作任务：1号主变由运行转检修\n调度指令号：DD-2026-001\n操作人：张三\n操作步骤：\n1. 断开10kV侧断路器\n2. 验电\n3. 挂地线\n安全措施：\n- 戴绝缘手套\n危险点：\n- 触电\n", "ticketType": "操作票", "expect": "pass"},
  {"text": "工作任务：2号线路检修\n调度指令号：DD-2026-002\n工作负责人：李四\n操作步骤：\n1. 停电\n2. 验电\n3. 接地\n危险点：\n- 高处坠落\n", "ticketType": "工作票", "expect": "pass"},
  {"text": "调度指令号：DD-2026-003\n操作人：王五\n操作步骤：\n1. 断开断路器\n2. 验电\n3. 挂地线\n危险点：\n- 触电\n", "ticketType": "操作票", "expect": "warn"},
  {"text": "操作任务：线路转检修\n调度指令号：DD-2026-004\n操作人：赵六\n操作步骤：\n1. 挂地线\n2. 验电\n危险点：\n- 触电\n", "ticketType": "操作票", "expect": "warn"},
  {"text": "操作任务：母线转检修\n调度指令号：DD-2026-005\n操作人：孙七\n操作步骤：\n1. 接地\n2. 验电\n危险点：\n- 触电\n", "ticketType": "操作票", "expect": "warn"},
  {"text": "调度指令号：DD-2026-006\n操作人：周八\n操作步骤：\n1. 挂地线\n2. 验电\n危险点：\n- 触电\n", "ticketType": "操作票", "expect": "fail"},
  {"text": "操作人：吴九\n操作步骤：\n1. 挂地线\n2. 验电\n危险点：\n- 触电\n", "ticketType": "操作票", "expect": "fail"},
  {"text": "操作任务：3号变转检修\n操作步骤：\n1. 挂地线\n2. 接地\n3. 验电\n危险点：\n- 触电\n", "ticketType": "操作票", "expect": "fail"},
  {"text": "操作任务：线路检修\n调度指令号：DD-2026-009\n约时停送电\n操作步骤：\n1. 停电\n2. 验电\n危险点：\n- 触电\n", "ticketType": "操作票", "expect": "warn"},
  {"text": "操作任务：4号变转检修\n调度指令号：DD-2026-010\n操作步骤：\n1. 停电\n2. 验电\n危险点：\n- 触电\n", "ticketType": "操作票", "expect": "pass"}
]
```
（说明：#6 缺任务+SEQ_001=2 critical→fail；#7 缺任务+缺调度+SEQ_001→fail；#8 SEQ_001+SEQ_002+缺调度+缺操作人→fail；#9 BLOCK_001+缺操作人=2 major→warn(70)；#10 缺操作人=1 major→pass(85)）

追加到 `tests/test_ticket_audit.py`：
```python
import json
from pathlib import Path

_GOLDEN_PATH = Path(__file__).resolve().parent.parent / "backend" / "data" / "golden_tickets.json"


def test_golden_tickets_regression(monkeypatch):
    """规则层确定性回归：LLM mock 为 []，每例 overall 必须等于 expect。"""
    async def no_llm(*a, **k): return []
    monkeypatch.setattr(svc, "_llm_check", no_llm)
    cases = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    assert len(cases) == 10
    for i, c in enumerate(cases):
        report = asyncio.run(svc.audit_ticket(c["text"], c["ticketType"], None))
        assert report["overall"] == c["expect"], (
            f"golden #{i} 期望 {c['expect']} 实得 {report['overall']} "
            f"(score={report['score']}, items={[it['ruleId'] for it in report['items']]})")
```

- [ ] **Step 2: 运行测试确认通过（数据与规则同步交付，无先红后绿）**

Run: `venv/Scripts/python.exe -m pytest tests/test_ticket_audit.py::test_golden_tickets_regression -v`
Expected: PASS（10 例 overall 全部命中 expect）
若某例不中：看断言打印的 score/items，按打分基线微调该例 text 或 expect（基线扣分值不得改）。

- [ ] **Step 3: 全量回归**

Run: `venv/Scripts/python.exe -m pytest tests/test_ticket_audit.py -v`
Expected: PASS（18 个测试全过）

- [ ] **Step 4: Commit**
```bash
git add backend/data/golden_tickets.json tests/test_ticket_audit.py
git commit -m "test(ticket-audit): 10 例 golden 票据回归门禁（规则层确定性）"
```

---

### Task 6: 前端 — api + Diagnose.vue 第 4 个 tab

**Files:**
- Modify: `frontend/src/api/index.js`（加 `auditTicket`）
- Modify: `frontend/src/views/Diagnose.vue`（第 4 个 tab + 结果渲染）

**Interfaces:**
- Consumes: Task 4 的 `POST /api/domain/ticket/audit`
- Produces: `auditTicket(ticketText, ticketType, modelType)`；Diagnose.vue 新增 tab，结果区渲染 overall/score/items

- [ ] **Step 1: api 加 auditTicket**

`frontend/src/api/index.js` 在 `generateTicket` 行之后追加：
```js
export const auditTicket = (ticketText, ticketType, modelType) =>
  request.post('/domain/ticket/audit', { ticketText, ticketType, modelType })
```

- [ ] **Step 2: Diagnose.vue 加第 4 个 tab**

模板：在 `<div class="tabs">` 内「📝 两票生成」按钮后追加：
```html
      <button class="tab" :class="{ active: tab === 'audit' }" @click="tab = 'audit'">🔍 两票审核</button>
```
在「两票生成」`<div class="card" v-show="tab === 'ticket'">` 之后追加第 4 张卡片：
```html
    <!-- 两票审核 -->
    <div class="card" v-show="tab === 'audit'">
      <div class="row" style="margin-bottom: 14px">
        <select class="select" v-model="auditType" style="max-width:120px"><option value="操作票">操作票</option><option value="工作票">工作票</option></select>
        <select class="select" v-model="modelType" style="max-width:140px"><option value="">默认模型</option><option value="deepseek">DeepSeek</option><option value="qwen">通义千问</option><option value="doubao">豆包</option></select>
        <button class="btn btn-primary" @click="doAudit" :disabled="auditLoading || !auditText.trim()">{{ auditLoading ? '审核中…' : '开始审核' }}</button>
      </div>
      <textarea class="input" v-model="auditText" placeholder="粘贴已填票据全文（含 任务/调度指令号/操作人/操作步骤/安全措施/危险点）" style="width:100%;min-height:160px;resize:vertical;font-family:inherit;margin-bottom:12px"></textarea>
      <div v-if="audit">
        <div class="audit-head">
          <span class="badge" :class="'ov-' + audit.overall">{{ {pass:'✓ 合规',warn:'⚠ 需修改',fail:'✗ 不合规'}[audit.overall] }}</span>
          <span class="score">得分 {{ audit.score }}</span>
          <span class="hint">· {{ audit.latencyMs }}ms</span>
        </div>
        <div v-if="!audit.items.length" class="empty">未发现问题</div>
        <div class="cause" v-for="(it, i) in audit.items" :key="i">
          <span class="lk" :class="'sv-' + it.severity">{{ {critical:'严',major:'重',minor:'轻'}[it.severity] }}</span>
          <div class="cause-body">
            <div class="cause-name">{{ it.msg }} <span class="hint">[{{ it.ruleId }} · {{ it.layer }}]</span></div>
            <div class="cause-line" v-if="it.suggestion"><b>建议：</b>{{ it.suggestion }}</div>
          </div>
        </div>
      </div>
    </div>
```

script：import 行改为：
```js
import { diagnose, similarCase, generateTicket, auditTicket } from '../api'
```
在 `const ticketTask = ...` 那段之后追加：
```js
const auditText = ref(''); const auditType = ref('操作票'); const auditLoading = ref(false); const audit = ref(null)
async function doAudit() {
  if (!auditText.value.trim()) return
  auditLoading.value = true; audit.value = null
  try { audit.value = (await auditTicket(auditText.value, auditType.value, modelType.value || null)).data }
  catch (e) { show('审核失败（需管理员权限）') } finally { auditLoading.value = false }
}
```

style（`<style scoped>` 内）追加：
```css
.audit-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.ov-pass { background: var(--success, #34c759); } .ov-warn { background: var(--warning); } .ov-fail { background: var(--danger); }
.ov-pass, .ov-warn, .ov-fail { color: #fff; }
.score { font-weight: 700; font-size: 15px; color: var(--text); }
.sv-critical { background: var(--danger); } .sv-major { background: var(--warning); } .sv-minor { background: var(--text-soft); }
```

- [ ] **Step 3: 构建验证 + 手动**

```bash
cd frontend && npm run build   # 预期 ✓ built，无 error
```
手动：以 admin 登录 → 诊断页 → 「🔍 两票审核」tab → 选操作票 → 粘贴一张顺序错的票 → 「开始审核」→ 总览 fail + items 列表（severity badge + ruleId + 建议）。普通用户点审核应 toast「审核失败（需管理员权限）」。

- [ ] **Step 4: Commit**
```bash
git add frontend/src/api/index.js frontend/src/views/Diagnose.vue
git commit -m "feat(ticket-audit): 前端两票审核 tab（Diagnose 第4页 + auditTicket API）"
```

---

## Self-Review（已自检）

- **Spec 覆盖**：
  - 双层架构（规则+LLM）→ Task 2 + Task 3 ✅
  - `parse_ticket` / `audit_ticket` / `_rule_check` / `_llm_check` → Task 1/2/3 ✅
  - 规则配置文件 + `_DEFAULT_RULES` 兜底 → Task 1 ✅（JSON 替 YAML，已标注偏离）
  - `POST /domain/ticket/audit`（admin）→ Task 4 ✅
  - `TICKET_AUDIT{result}` 指标 → Task 4 ✅
  - 降级 `degraded("ticket_audit_llm", e)` → Task 3 ✅
  - 解析为空 fail 短路 → Task 3 ✅
  - 前端 Diagnose 第 4 tab → Task 6 ✅
  - golden 回归集 10 例 → Task 5 ✅
  - 报告结构 `{overall,score,ticketType,items,latencyMs}` → Task 3 ✅
  - YAGNI（不做 OCR/外部对接/规则编辑器）→ 未引入 ✅

- **类型一致**：`parse_ticket` 输出字段（task/dispatch_no/operator/steps/safety/dangers/raw）在 Task 1 定义，Task 2 `_rule_check`、Task 3 `_llm_check`/`audit_ticket` 消费一致；item 结构 `{layer,ruleId,type,severity,msg,suggestion}` 全链路一致；`audit_ticket -> {overall,score,ticketType,items,latencyMs}` 与前端 Task 6 渲染字段（overall/score/latencyMs/items[].severity/msg/ruleId/suggestion/layer）一致 ✅

- **无占位符**：每步含实代码 / 实命令 / 实预期；无 TBD/TODO/"类似 Task N" ✅

- **偏离 spec 已标注**：规则文件 YAML→JSON（Global Constraints 首条，避免新依赖）；测试目录 `backend/tests/`→`tests/`（Global Constraints）✅
