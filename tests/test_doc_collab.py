"""文档协作批注 + 电子签批单测：批注 CRUD、会签流、口令复核、哈希链防篡改。"""
import pytest

from app.core.response import BizError
from app.core.security import hash_password
from app.models.document import Document
from app.models.user import User
from app.services import doc_annotation_service as ann
from app.services import doc_signoff_service as signoff

pytestmark = pytest.mark.asyncio


async def _seed(test_db):
    doc = Document(id="d1", doc_name="安规.pdf", minio_object="k1", status="vectorized")
    zhang = User(id="u1", username="zhang", password_hash=hash_password("Pass123!"),
                 role="editor", tenant_id="default")
    li = User(id="u2", username="li", password_hash=hash_password("Pass123!"),
              role="operator", tenant_id="default")
    wang = User(id="u3", username="wang", password_hash=hash_password("Wang456!"),
                role="editor", tenant_id="default")
    test_db.add_all([doc, zhang, li, wang])
    await test_db.commit()
    return zhang, li, wang


# ---------- 批注 ----------

async def test_annotation_lifecycle(test_db):
    zhang, li, _ = await _seed(test_db)
    a = await ann.create_annotation(test_db, "d1", 3, "第3.2条 原文", "此条与新版冲突",
                                    "default", "zhang", "", "editor")
    assert a["status"] == "open"
    r = await ann.reply_annotation(test_db, a["id"], "确认，已同步修订", "default", "li")
    assert len(r["replies"]) == 1 and r["replies"][0]["author"] == "li"
    resolved = await ann.resolve_annotation(test_db, a["id"], True, "default", "li")
    assert resolved["status"] == "resolved" and resolved["resolvedBy"] == "li"
    stats = await ann.annotation_stats(test_db, "d1", "default")
    assert stats == {"total": 1, "open": 0, "resolved": 1}
    rows = await ann.list_annotations(test_db, "d1", "default", "open", "", "editor")
    assert rows == []  # 已解决的不出现在 open 过滤


async def test_annotation_delete_permission(test_db):
    zhang, li, wang = await _seed(test_db)
    a = await ann.create_annotation(test_db, "d1", 0, "", "内容", "default",
                                    "zhang", "", "editor")
    with pytest.raises(BizError):  # 非作者非 admin
        await ann.delete_annotation(test_db, a["id"], "default", "li", is_admin=False)
    await ann.delete_annotation(test_db, a["id"], "default", "zhang", is_admin=False)


# ---------- 签批 ----------

async def test_signoff_full_flow_and_chain(test_db):
    zhang, li, wang = await _seed(test_db)
    s = await signoff.create_signoff(
        test_db, "d1", "", [{"signer": "li", "role": "operator"},
                            {"signer": "wang", "role": "editor"}],
        "default", "zhang", "", "editor")
    assert s["status"] == "draft" and len(s["flow"]) == 2

    sub = await signoff.submit_signoff(test_db, s["id"], "default", "zhang")
    assert sub["status"] == "pending" and sub["currentSeq"] == 1
    assert sub["docFingerprint"]  # 提交时固化指纹

    # 非当前节点签批人 → 403
    with pytest.raises(BizError):
        await signoff.sign(test_db, s["id"], "default", zhang, "Pass123!")
    # 口令错误 → 403
    with pytest.raises(BizError):
        await signoff.sign(test_db, s["id"], "default", li, "wrong-pass")

    r1 = await signoff.sign(test_db, s["id"], "default", li, "Pass123!", "同意")
    assert r1["currentSeq"] == 2 and r1["status"] == "pending"
    r2 = await signoff.sign(test_db, s["id"], "default", wang, "Wang456!")
    assert r2["status"] == "signed" and r2["finishedAt"]

    v = await signoff.verify_signoff(test_db, s["id"], "default")
    assert v["valid"] is True and v["chainOk"] is True and v["docChanged"] is False

    detail = await signoff.get_signoff_detail(test_db, s["id"], "default", "", "editor")
    actions = [e["action"] for e in detail["events"]]
    assert actions == ["create", "submit", "sign", "sign"]


async def test_signoff_chain_detects_tamper(test_db, monkeypatch):
    """改掉已签节点的意见 → 哈希链校验失败。"""
    from sqlalchemy import select

    from app.models.doc_collab import DocSignoff
    zhang, li, _ = await _seed(test_db)
    s = await signoff.create_signoff(test_db, "d1", "", [{"signer": "li"}],
                                     "default", "zhang", "", "editor")
    await signoff.submit_signoff(test_db, s["id"], "default", "zhang")
    await signoff.sign(test_db, s["id"], "default", li, "Pass123!", "原始意见")
    # 直接改库模拟事后篡改
    row = (await test_db.execute(select(DocSignoff).where(
        DocSignoff.id == s["id"]))).scalar_one()
    row.flow[0]["comment"] = "被篡改的意见"
    row.flow = list(row.flow)
    await test_db.commit()
    v = await signoff.verify_signoff(test_db, s["id"], "default")
    assert v["chainOk"] is False


async def test_signoff_reject_and_cancel(test_db):
    zhang, li, _ = await _seed(test_db)
    # 驳回终止
    s = await signoff.create_signoff(test_db, "d1", "", [{"signer": "li"}, {"signer": "wang"}],
                                     "default", "zhang", "", "editor")
    await signoff.submit_signoff(test_db, s["id"], "default", "zhang")
    r = await signoff.reject(test_db, s["id"], "default", li, "Pass123!", "内容过期")
    assert r["status"] == "rejected"
    # 发起人取消草稿
    s2 = await signoff.create_signoff(test_db, "d1", "", [{"signer": "li"}],
                                      "default", "zhang", "", "editor")
    c = await signoff.cancel_signoff(test_db, s2["id"], "default", "zhang", is_admin=False)
    assert c["status"] == "cancelled"
    # 非发起人不可取消
    s3 = await signoff.create_signoff(test_db, "d1", "", [{"signer": "li"}],
                                      "default", "zhang", "", "editor")
    with pytest.raises(BizError):
        await signoff.cancel_signoff(test_db, s3["id"], "default", "li", is_admin=False)


async def test_my_pending_and_flow_validation(test_db):
    zhang, li, wang = await _seed(test_db)
    with pytest.raises(BizError):  # 空节点
        await signoff.create_signoff(test_db, "d1", "", [], "default", "zhang", "", "editor")
    with pytest.raises(BizError):  # 重复签批人
        await signoff.create_signoff(
            test_db, "d1", "", [{"signer": "li"}, {"signer": "li"}],
            "default", "zhang", "", "editor")
    s = await signoff.create_signoff(test_db, "d1", "", [{"signer": "li"}, {"signer": "wang"}],
                                     "default", "zhang", "", "editor")
    await signoff.submit_signoff(test_db, s["id"], "default", "zhang")
    pending = await signoff.my_pending(test_db, "default", "li")
    assert len(pending) == 1 and pending[0]["id"] == s["id"]
    assert await signoff.my_pending(test_db, "default", "wang") == []
