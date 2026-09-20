"""通知中心 + 第二批联动功能单测。"""
import pytest
from contextlib import asynccontextmanager

from app.core.response import BizError
from app.models.document import Document
from app.models.user import User
from app.services import notification_service as notif

pytestmark = pytest.mark.asyncio


@pytest.fixture
def patch_session(monkeypatch):
    """把 notify_user/sweep 内部自开的 AsyncSessionLocal 指到测试 sqlite session。"""
    def _patch(test_db):
        @asynccontextmanager
        async def _fake_session():
            yield test_db
        monkeypatch.setattr("app.db.session.AsyncSessionLocal", _fake_session)
    return _patch


async def test_notify_user_persist_and_list(test_db, patch_session):
    patch_session(test_db)
    test_db.add(User(id="u9", username="zhang", password_hash="x",
                     role="editor", tenant_id="default"))
    await test_db.commit()

    nid = await notif.notify_user("zhang", "signoff_pending", "轮到你签批：会签A",
                                  body="节点 1 等你", link="/doc-collab?doc=d1")
    assert nid
    rows = await notif.list_notifications(test_db, "zhang", "default")
    assert len(rows) == 1 and rows[0]["title"].startswith("轮到你签批")
    assert rows[0]["read"] is False

    assert await notif.unread_count(test_db, "zhang", "default") == 1
    await notif.mark_read(test_db, nid, "zhang", "default")
    assert await notif.unread_count(test_db, "zhang", "default") == 0
    # 他人不可标记（通知不存在于该用户域）
    with pytest.raises(BizError):
        await notif.mark_read(test_db, nid, "li", "default")


async def test_mark_all_read(test_db):
    test_db.add(User(id="u8", username="li", password_hash="x",
                     role="operator", tenant_id="default"))
    await test_db.commit()
    for i in range(3):
        test_db.add(notif.Notification(recipient="li", type="system",
                                       title=f"t{i}", tenant="default"))
    await test_db.commit()
    assert await notif.unread_count(test_db, "li", "default") == 3
    res = await notif.mark_all_read(test_db, "li", "default")
    assert res["marked"] == 3
    assert await notif.unread_count(test_db, "li", "default") == 0


async def test_notify_many_dedup(test_db, patch_session):
    patch_session(test_db)
    test_db.add(User(id="u7", username="w1", password_hash="x",
                     role="editor", tenant_id="default"))
    await test_db.commit()
    n = await notif.notify_many(["w1", "w1", "w1"], "report_done", "周报完成")
    assert n == 1
    assert await notif.unread_count(test_db, "w1", "default") == 1


async def test_annotation_to_governance_issue(test_db):
    from sqlalchemy import select

    from app.models.knowledge_governance import KnowledgeGovernanceIssue
    from app.services import doc_annotation_service as ann

    test_db.add(Document(id="d9", doc_name="安规9.pdf", minio_object="k9",
                         status="vectorized"))
    await test_db.commit()
    a = await ann.create_annotation(test_db, "d9", 2, "第2条", "此条与新版规程冲突",
                                    "default", "zhang", "", "editor")
    r1 = await ann.to_governance_issue(test_db, a["id"], "default", "zhang")
    assert r1["existing"] is False and r1["issueId"]
    issue = (await test_db.execute(select(KnowledgeGovernanceIssue).where(
        KnowledgeGovernanceIssue.id == r1["issueId"]))).scalar_one()
    assert issue.issue_type == "annotation_comment" and issue.doc_id == "d9"
    assert "此条与新版规程冲突" in issue.summary
    # 幂等：同版本再转 → existing
    r2 = await ann.to_governance_issue(test_db, a["id"], "default", "zhang")
    assert r2["existing"] is True and r2["issueId"] == r1["issueId"]


async def test_summarized_history_for_prompt(test_db, monkeypatch):
    from app.config import settings
    from app.models.conversation import Conversation
    from app.services import conversation_summary as cs

    monkeypatch.setattr(settings, "CONV_SUMMARY_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "CONV_SUMMARY_THRESHOLD", 4, raising=False)
    monkeypatch.setattr(settings, "CONV_SUMMARY_KEEP_LAST", 2, raising=False)

    conv = Conversation(id="cv1", username="zhang", title="t")
    test_db.add(conv)
    await test_db.commit()
    history = [{"role": "user", "content": f"q{i}"} for i in range(6)]

    # 未超阈值 → 原样
    short = await cs.summarized_history_for_prompt(test_db, "cv1", history[:3])
    assert short == history[:3]

    # 超阈值 + 无摘要 → 原样返回（后台摘要另跑，不阻塞）
    async def _noop(db, cid, msgs):
        return None
    monkeypatch.setattr(cs, "summarize_conversation", _noop)
    full = await cs.summarized_history_for_prompt(test_db, "cv1", history)
    assert full == history

    # 有摘要 → 摘要头 + 最近 2 条
    conv.summary = "此前讨论了1号主变油温高的处置"
    await test_db.commit()
    trimmed = await cs.summarized_history_for_prompt(test_db, "cv1", history)
    assert len(trimmed) == 3
    assert trimmed[0]["content"].startswith("[此前对话摘要")
    assert trimmed[-1] == history[-1]

    # 开关关 → 原样（默认现状零改动）
    monkeypatch.setattr(settings, "CONV_SUMMARY_ENABLE", False, raising=False)
    assert await cs.summarized_history_for_prompt(test_db, "cv1", history) == history


async def test_drill_leaderboard_and_sweep(test_db, monkeypatch):
    from datetime import datetime, timedelta

    from app.config import settings
    from app.services import drill_service as svc

    monkeypatch.setattr(settings, "DRILL_MAX_DURATION_SECONDS", 10, raising=False)

    def _prop():
        return [{"tOffset": 0, "device": "d", "severity": "critical", "event": "e"}]

    def _chk():
        return [{"id": "c1", "action": "查看遥测", "keywords": ["遥测"]}]

    # 用户 A：一场全覆盖；用户 B：一场半覆盖
    for user, cov in (("alice", 1.0), ("bob", 0.0)):
        s = await svc.create_scenario(
            test_db, "default", user, f"剧本-{user}", "", "", "d1", "",
            _prop(), _chk())
        r = await svc.start_run(test_db, "default", user, s["id"])
        if cov == 1.0:
            await svc.record_action(test_db, r["id"], "default", user, "查看遥测")
        await svc.finish_run(test_db, r["id"], "default", user, with_llm=False)

    lb = await svc.leaderboard(test_db, "default", days=30)
    assert [u["user"] for u in lb] == ["alice", "bob"]
    assert lb[0]["runs"] == 1 and lb[0]["avgCoverage"] == 1.0
    assert lb[1]["avgCoverage"] == 0.0

    # sweep：跑一场 started_at 拨到过去的 running 演练 → 后台结算 finished
    s2 = await svc.create_scenario(test_db, "default", "carol", "超时剧本", "", "",
                                   "d1", "", _prop(), _chk())
    r2 = await svc.start_run(test_db, "default", "carol", s2["id"])
    from sqlalchemy import select

    from app.models.drill import DrillRun
    row = (await test_db.execute(select(DrillRun).where(
        DrillRun.id == r2["id"]))).scalar_one()
    row.started_at = datetime.now() - timedelta(seconds=9999)
    await test_db.commit()

    # notify_user 内部自开 session → 指向测试库
    @asynccontextmanager
    async def _fake_session():
        yield test_db
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", _fake_session)

    n = await svc.sweep_timeout_runs("default")
    assert n == 1
    out = await svc.run_state(test_db, r2["id"], "default")
    assert out["status"] == "finished"


def test_eval_matrix_report_safety():
    """报告浏览：白名单正则防路径穿越。"""
    from app.services import eval_matrix_service as em

    with pytest.raises(BizError):
        em.read_report("../docker-compose.yml")  # 穿越被拒
    with pytest.raises(BizError):
        em.read_report("secret.md")  # 非 eval_matrix_ 前缀被拒
    reports = em.list_reports()
    assert all(r["name"].startswith("eval_matrix_") for r in reports)
