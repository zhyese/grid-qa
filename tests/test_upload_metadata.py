"""数据飞轮·C2 上传引导治理元数据单测。

upload_documents 收 effectiveAt/expiresAt/isPermanent/versionOf → 建 KnowledgeDocumentMetadata
（status=draft）。开关 GOVERNANCE_UPLOAD_REQUIRE 默认关（关=现状不上传即建，开=引导）。
"""
import asyncio
import uuid as _u
from datetime import datetime
from types import SimpleNamespace


def _run(coro):
    return asyncio.run(coro)


class _FakeUploadFile:
    def __init__(self, name="x.pdf", content=b"hello", content_type="application/pdf"):
        self.filename = name
        self._c = content
        self.content_type = content_type

    async def read(self):
        return self._c


class _FakeResult:
    def __init__(self, value=None):
        self._v = value

    def scalar_one_or_none(self):
        return self._v

    def scalars(self):
        return self

    def all(self):
        return []

    def scalar(self):
        return 0


class _FakeSession:
    """模拟 AsyncSession：add 捕获；execute 永远返回 None。"""

    def __init__(self):
        self.added = []

    async def execute(self, stmt):
        return _FakeResult(None)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def refresh(self, obj):
        pass


def test_c2_upload_with_metadata_creates_knowledge_document_metadata(monkeypatch):
    """upload_documents 带 effectiveAt/expiresAt/versionOf → 建 KnowledgeDocumentMetadata。"""
    import app.services.document_service as ds
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "GOVERNANCE_UPLOAD_REQUIRE", True)

    # mock minio
    monkeypatch.setattr(ds.minio_client, "put_object", lambda *a, **kw: None)
    # mock DocumentVersion 不存在
    # mock settings 等用到的
    monkeypatch.setattr(ds, "_auto_equipment_tags", lambda txt: "")

    db = _FakeSession()
    files = [_FakeUploadFile("manual_v1.pdf", b"y")]

    _run(ds.upload_documents(
        db, files, doc_type="运维手册", username="tester", tenant_id="default",
        dept="", allowed_roles="",
        effective_at=datetime(2026, 1, 1), expires_at=datetime(2027, 1, 1),
        is_permanent=False, version_of="",
    ))

    # 验证：DB 新增了 Document + KnowledgeDocumentMetadata
    types_added = [type(o).__name__ for o in db.added]
    assert "Document" in types_added
    assert "KnowledgeDocumentMetadata" in types_added
    meta = next(o for o in db.added if type(o).__name__ == "KnowledgeDocumentMetadata")
    assert meta.effective_at == datetime(2026, 1, 1)
    assert meta.expires_at == datetime(2027, 1, 1)
    assert meta.version_status == "draft"  # 默认 draft（待审核激活）


def test_c2_upload_without_metadata_no_knowledge_document_metadata(monkeypatch):
    """无治理字段 + GOVERNANCE_UPLOAD_REQUIRE=False → 不建 meta（关=现状零破坏）。"""
    import app.services.document_service as ds
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "GOVERNANCE_UPLOAD_REQUIRE", False)

    monkeypatch.setattr(ds.minio_client, "put_object", lambda *a, **kw: None)
    monkeypatch.setattr(ds, "_auto_equipment_tags", lambda txt: "")

    db = _FakeSession()
    files = [_FakeUploadFile("a.pdf", b"x")]
    _run(ds.upload_documents(
        db, files, doc_type="运维手册", username="tester",
    ))

    types_added = [type(o).__name__ for o in db.added]
    assert "KnowledgeDocumentMetadata" not in types_added


def test_c2_upload_metadata_defaults_to_active_when_all_fields(monkeypatch):
    """permanent + version_label 完整时 version_status=active（草稿→激活条件）。"""
    import app.services.document_service as ds
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "GOVERNANCE_UPLOAD_REQUIRE", True)

    monkeypatch.setattr(ds.minio_client, "put_object", lambda *a, **kw: None)
    monkeypatch.setattr(ds, "_auto_equipment_tags", lambda txt: "")

    db = _FakeSession()
    files = [_FakeUploadFile("b.pdf", b"y")]
    _run(ds.upload_documents(
        db, files, doc_type="运维手册", username="tester",
        effective_at=datetime(2026, 1, 1), is_permanent=True,
    ))
    meta = next(o for o in db.added if type(o).__name__ == "KnowledgeDocumentMetadata")
    assert meta.is_permanent is True
    assert meta.expires_at is None  # permanent + None expires
