"""文档协作批注 + 电子签批模型（DOC_COLLAB_ENABLE 门控）。

- doc_annotations  锚点批注：chunk_idx + quote 双锚（ quote 供前端高亮定位/回显），
                   replies JSON 内嵌（轻量讨论，不建回复子表）
- doc_signoffs     签批流：flow JSON 存节点序列（顺序会签），
                   doc_fingerprint 提交时刻的文档指纹（updated_at+file_size，篡改可检测），
                   hash_chain 校验串 = 每节点签名哈希链（sha256(prev+signer+ts)，防事后篡改）
- doc_signoff_events 审计事件：发起/提交/签批/驳回/取消全留痕（电子签批合规要求）
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DocAnnotation(Base):
    __tablename__ = "doc_annotations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    doc_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    chunk_idx: Mapped[int] = mapped_column(Integer, default=0)          # 锚点 chunk 序号
    quote: Mapped[str] = mapped_column(String(512), default="")          # 锚点原文（截断 500 字）
    content: Mapped[str] = mapped_column(Text, nullable=False)           # 批注内容
    # open | resolved
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    replies: Mapped[list] = mapped_column(JSON, default=list)            # [{author, content, at}]
    resolved_by: Mapped[str] = mapped_column(String(64), default="")
    resolved_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    author: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DocSignoff(Base):
    __tablename__ = "doc_signoffs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    doc_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    # draft | pending | signed | rejected | cancelled
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    # [{seq, signer, role, status(pending|signed|rejected), signedAt, comment, signatureHash}]
    flow: Mapped[list] = mapped_column(JSON, default=list)
    current_seq: Mapped[int] = mapped_column(Integer, default=0)         # 当前待签节点 seq（0=未开始）
    # 提交时刻文档指纹 sha256(doc_id:updated_at:file_size)；verify 时重算比对，不一致=文档已变更
    doc_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    # 哈希链尾（verify 链路：h0=sha256(signoff_id)，node_i=sha256(prev+signer+ts+comment)）
    hash_chain: Mapped[str] = mapped_column(String(64), default="")
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class DocSignoffEvent(Base):
    __tablename__ = "doc_signoff_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signoff_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # submit | sign | reject | cancel | create
    action: Mapped[str] = mapped_column(String(16), default="")
    actor: Mapped[str] = mapped_column(String(64), default="")
    node_seq: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str] = mapped_column(String(500), default="")
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
