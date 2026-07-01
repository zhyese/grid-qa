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
