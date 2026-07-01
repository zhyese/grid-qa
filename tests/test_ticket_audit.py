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
