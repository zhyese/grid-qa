"""两票智能审核单测：解析 / 规则引擎 / LLM语义 / 聚合 / 端到端。"""
import asyncio
import json
from pathlib import Path
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
    p2 = _parsed(dispatch_no="DD-2026-001")
    assert "DISP_001" not in [it["ruleId"] for it in svc._rule_check(p2, svc._DEFAULT_RULES)]


def test_rule_blocklist():
    p = _parsed(steps=["约时停送电送电"], raw="约时停送电", dangers=["d"])
    assert any(it["ruleId"] == "BLOCK_001" for it in svc._rule_check(p, svc._DEFAULT_RULES))
    p2 = _parsed(steps=["正常操作"], raw="正常操作", dangers=["d"])
    assert "BLOCK_001" not in [it["ruleId"] for it in svc._rule_check(p2, svc._DEFAULT_RULES)]


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
    bad = "操作任务：1号主变由运行转检修\n调度指令号：DD-2026-001\n操作步骤：\n1. 验电\n2. 挂地线\n危险点：\n- 触电\n"  # 缺操作人 → REQ_OPERATOR
    report = asyncio.run(svc.audit_ticket(bad, "操作票", None))
    assert report["overall"] in ("pass", "warn", "fail")
    assert report["items"], "降级应仍返回规则层结果"
    assert all(it["layer"] == "rule" for it in report["items"]), "LLM 失败不应有 llm 项"


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
