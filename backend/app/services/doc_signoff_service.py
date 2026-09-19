"""电子签批服务：顺序会签流 + 口令复核 + 文档指纹 + 签名哈希链（防篡改可审计）。

签批动作 = 指定签批人 + 登录口令复核 + 时间戳，签名哈希链逐节点串联
（h0=sha256(signoff_id)，node_i=sha256(prev|signer|ts|comment)），事后改任何
节点内容 verify 即断链。文档指纹在提交时刻固化，文档变更后 verify 报 doc_changed。
不引入 CA/UKey 体系——口令复核即"电子签批"在本系统的合规实现。
"""
import hashlib
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import BizError
from app.core.security import verify_password
from app.models.doc_collab import DocSignoff, DocSignoffEvent
from app.models.user import User
from app.services import document_service


def _row(s: DocSignoff, with_events: bool = False, events: list | None = None) -> dict:
    d = {"id": s.id, "docId": s.doc_id, "title": s.title, "status": s.status,
         "flow": s.flow or [], "currentSeq": s.current_seq,
         "docFingerprint": s.doc_fingerprint, "hashChain": s.hash_chain,
         "createdBy": s.created_by,
         "createdAt": s.created_at.isoformat() if s.created_at else None,
         "updatedAt": s.updated_at.isoformat() if s.updated_at else None,
         "finishedAt": s.finished_at.isoformat() if s.finished_at else None}
    if with_events:
        d["events"] = [
            {"action": e.action, "actor": e.actor, "nodeSeq": e.node_seq,
             "detail": e.detail,
             "at": e.created_at.isoformat() if e.created_at else None}
            for e in (events or [])]
    return d


def _fingerprint(doc) -> str:
    # Document 无 updated_at 列，用 内容指纹（file_size/chunk_count）+ 状态 组合：
    # 文档重新解析/向量化/换文件都会改变指纹，从而让已提交的签批 verify 报 doc_changed。
    raw = f"{doc.id}:{doc.status}:{doc.file_size or 0}:{doc.chunk_count or 0}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _node_hash(prev: str, signer: str, ts: str, comment: str) -> str:
    raw = f"{prev}|{signer}|{ts}|{comment or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _check_doc(db: AsyncSession, doc_id: str, tenant_id: str):
    doc = await document_service.get_document(db, doc_id)
    if doc.tenant_id and doc.tenant_id != tenant_id:
        raise BizError("文档不存在", 404)
    return doc


async def _get_signoff(db: AsyncSession, signoff_id: str, tenant_id: str) -> DocSignoff:
    s = (await db.execute(select(DocSignoff).where(
        DocSignoff.id == signoff_id, DocSignoff.tenant == tenant_id))).scalar_one_or_none()
    if s is None:
        raise BizError("签批单不存在", 404)
    return s


async def _log(db: AsyncSession, s: DocSignoff, action: str, actor: str,
               node_seq: int = 0, detail: str = "") -> None:
    db.add(DocSignoffEvent(signoff_id=s.id, action=action, actor=actor,
                           node_seq=node_seq, detail=(detail or "")[:500],
                           tenant=s.tenant))


def _validate_flow(signers: list[dict]) -> list[dict]:
    """校验签批节点：非空、签批人唯一、角色合法。"""
    from app.core.permissions import VALID_ROLES

    if not signers or len(signers) > 10:
        raise BizError("签批节点需 1-10 个", 400)
    seen: set[str] = set()
    nodes = []
    for i, item in enumerate(signers, 1):
        signer = (item.get("signer") or "").strip()
        role = (item.get("role") or "").strip()
        if not signer:
            raise BizError(f"第 {i} 个节点签批人为空", 400)
        if signer in seen:
            raise BizError(f"签批人重复：{signer}", 400)
        seen.add(signer)
        if role and role not in VALID_ROLES:
            raise BizError(f"非法角色：{role}", 400)
        nodes.append({"seq": i, "signer": signer, "role": role,
                      "status": "pending", "signedAt": "", "comment": "",
                      "signatureHash": ""})
    return nodes


async def create_signoff(db: AsyncSession, doc_id: str, title: str, signers: list[dict],
                         tenant_id: str, username: str, user_dept: str,
                         user_role: str) -> dict:
    await _check_doc(db, doc_id, tenant_id)
    # 发起人需要对文档有管理权（ACL），防止对无权文档发起会签
    doc = await document_service.get_document(db, doc_id)
    document_service._assert_acl(doc, user_dept, user_role)
    nodes = _validate_flow(signers)
    s = DocSignoff(id=uuid.uuid4().hex, doc_id=doc_id,
                   title=(title or "")[:200] or f"文档会签·{doc.doc_name[:60]}",
                   status="draft", flow=nodes, current_seq=0,
                   tenant=tenant_id, created_by=username)
    db.add(s)
    await _log(db, s, "create", username, 0, f"{len(nodes)} 节点会签流创建")
    await db.commit()
    await db.refresh(s)  # MySQL 无 RETURNING，server_default 列需 refresh 才可读
    return _row(s)


async def submit_signoff(db: AsyncSession, signoff_id: str, tenant_id: str,
                         username: str) -> dict:
    s = await _get_signoff(db, signoff_id, tenant_id)
    if s.status != "draft":
        raise BizError("仅草稿状态可提交", 400)
    doc = await _check_doc(db, s.doc_id, tenant_id)
    # 提交时刻固化文档指纹 + 哈希链创世值
    s.doc_fingerprint = _fingerprint(doc)
    s.hash_chain = hashlib.sha256(s.id.encode("utf-8")).hexdigest()
    s.status = "pending"
    s.current_seq = 1
    await _log(db, s, "submit", username, 0, "提交会签，指纹已固化")
    await db.commit()
    await db.refresh(s)  # onupdate(updated_at) 后属性过期，async session 禁属性级懒 IO
    return _row(s)


async def sign(db: AsyncSession, signoff_id: str, tenant_id: str, user: User,
               password: str, comment: str = "") -> dict:
    """签批：当前节点签批人 + 口令复核，通过后推进哈希链。"""
    s = await _get_signoff(db, signoff_id, tenant_id)
    if s.status != "pending":
        raise BizError(f"当前状态 {s.status} 不可签批", 400)
    if s.current_seq < 1 or s.current_seq > len(s.flow):
        raise BizError("签批流已走完", 400)
    # JSON 列不跟踪原地修改且浅拷贝重赋值内容相等不落库 → 必须深拷贝节点改完整体赋回
    flow = [dict(n) for n in s.flow]
    node = next((n for n in flow if n["seq"] == s.current_seq), None)
    if node is None:
        raise BizError("签批流节点缺失", 500)
    if node["signer"] != user.username:
        raise BizError(f"当前节点签批人为 {node['signer']}，非本人", 403)
    if not password or not verify_password(password, user.password_hash):
        raise BizError("口令复核失败，电子签批要求重新验证口令", 403)
    ts = datetime.now().isoformat()
    node["status"] = "signed"
    node["signedAt"] = ts
    node["comment"] = (comment or "")[:500]
    node["signatureHash"] = _node_hash(s.hash_chain, node["signer"], ts, node["comment"])
    s.flow = flow
    s.hash_chain = node["signatureHash"]
    if s.current_seq >= len(flow):
        s.status = "signed"
        s.finished_at = datetime.now()
    else:
        s.current_seq += 1
    await _log(db, s, "sign", user.username, node["seq"], f"节点 {node['seq']} 签批通过")
    await db.commit()
    await db.refresh(s)  # onupdate(updated_at) 后属性过期，async session 禁属性级懒 IO
    return _row(s)


async def reject(db: AsyncSession, signoff_id: str, tenant_id: str, user: User,
                 password: str, reason: str) -> dict:
    """驳回：当前节点签批人可驳回，整单终止（rejected），可重新发起。"""
    s = await _get_signoff(db, signoff_id, tenant_id)
    if s.status != "pending":
        raise BizError(f"当前状态 {s.status} 不可驳回", 400)
    flow = [dict(n) for n in s.flow]  # 深拷贝后改，再整体赋回（JSON 列变更才落库）
    node = next((n for n in flow if n["seq"] == s.current_seq), None)
    if node is None or node["signer"] != user.username:
        raise BizError("仅当前节点签批人可驳回", 403)
    if not password or not verify_password(password, user.password_hash):
        raise BizError("口令复核失败，电子签批要求重新验证口令", 403)
    ts = datetime.now().isoformat()
    node["status"] = "rejected"
    node["signedAt"] = ts
    node["comment"] = (reason or "")[:500]
    node["signatureHash"] = _node_hash(s.hash_chain, node["signer"], ts, node["comment"])
    s.flow = flow
    s.hash_chain = node["signatureHash"]
    s.status = "rejected"
    s.finished_at = datetime.now()
    await _log(db, s, "reject", user.username, node["seq"],
               f"节点 {node['seq']} 驳回：{node['comment']}")
    await db.commit()
    await db.refresh(s)  # onupdate(updated_at) 后属性过期，async session 禁属性级懒 IO
    return _row(s)


async def cancel_signoff(db: AsyncSession, signoff_id: str, tenant_id: str,
                         username: str, is_admin: bool) -> dict:
    """取消：发起人或 admin，draft/pending 均可取消。"""
    s = await _get_signoff(db, signoff_id, tenant_id)
    if s.status in ("signed", "rejected", "cancelled"):
        raise BizError("已终结的签批单不可取消", 400)
    if not is_admin and s.created_by != username:
        raise BizError("仅发起人或管理员可取消", 403)
    s.status = "cancelled"
    s.finished_at = datetime.now()
    await _log(db, s, "cancel", username, 0, "签批单取消")
    await db.commit()
    await db.refresh(s)  # onupdate(updated_at) 后属性过期，async session 禁属性级懒 IO
    return _row(s)


async def list_signoffs(db: AsyncSession, doc_id: str, tenant_id: str,
                        user_dept: str, user_role: str) -> list[dict]:
    await _check_doc(db, doc_id, tenant_id)
    document_service._assert_acl(
        await document_service.get_document(db, doc_id), user_dept, user_role)
    rows = (await db.execute(
        select(DocSignoff).where(DocSignoff.doc_id == doc_id,
                                 DocSignoff.tenant == tenant_id)
        .order_by(DocSignoff.created_at.desc()).limit(100))).scalars().all()
    return [_row(s) for s in rows]


async def my_pending(db: AsyncSession, tenant_id: str, username: str) -> list[dict]:
    """工作台：轮到我签的签批单（flow 是 JSON 列，取 pending 单后内存过滤）。"""
    rows = (await db.execute(
        select(DocSignoff).where(DocSignoff.tenant == tenant_id,
                                 DocSignoff.status == "pending")
        .order_by(DocSignoff.updated_at.desc()).limit(200))).scalars().all()
    out = []
    for s in rows:
        node = next((n for n in (s.flow or []) if n["seq"] == s.current_seq), None)
        if node and node["signer"] == username:
            out.append(_row(s))
    return out


async def get_signoff_detail(db: AsyncSession, signoff_id: str, tenant_id: str,
                             user_dept: str, user_role: str) -> dict:
    s = await _get_signoff(db, signoff_id, tenant_id)
    doc = await _check_doc(db, s.doc_id, tenant_id)
    document_service._assert_acl(doc, user_dept, user_role)
    events = (await db.execute(
        select(DocSignoffEvent).where(DocSignoffEvent.signoff_id == signoff_id)
        .order_by(DocSignoffEvent.id))).scalars().all()
    return _row(s, with_events=True, events=events)


async def verify_signoff(db: AsyncSession, signoff_id: str, tenant_id: str) -> dict:
    """完整性校验：重算文档指纹 + 哈希链，报告是否被篡改。"""
    s = await _get_signoff(db, signoff_id, tenant_id)
    doc_changed = False
    if s.doc_fingerprint:
        try:
            doc = await document_service.get_document(db, s.doc_id)
            doc_changed = _fingerprint(doc) != s.doc_fingerprint
        except BizError:
            doc_changed = True  # 文档已被删除 → 指纹无处比对，视为变更
    chain_ok = True
    prev = hashlib.sha256(s.id.encode("utf-8")).hexdigest()
    for node in (s.flow or []):
        if node.get("status") not in ("signed", "rejected"):
            continue  # 未签节点无哈希
        expect = _node_hash(prev, node["signer"], node.get("signedAt", ""),
                            node.get("comment", ""))
        if expect != node.get("signatureHash"):
            chain_ok = False
            break
        prev = expect
    # 链尾比对：所有已签节点重算完后应等于存储的 hash_chain（无已签节点时 = 创世值）
    chain_tail_ok = prev == s.hash_chain if s.hash_chain else True
    return {"id": s.id, "status": s.status, "docChanged": doc_changed,
            "chainOk": chain_ok and chain_tail_ok,
            "valid": chain_ok and chain_tail_ok and not doc_changed,
            "checkedAt": datetime.now().isoformat()}


async def suggest_signers(db: AsyncSession, tenant_id: str) -> list[dict]:
    """发起会签时的签批人下拉：真实存在的用户（含角色）。"""
    rows = (await db.execute(
        select(User.username, User.role, User.dept)
        .where(User.tenant_id == tenant_id, User.status == "active")
        .order_by(User.username).limit(100))).all()
    return [{"signer": u, "role": r, "dept": d} for u, r, d in rows]
