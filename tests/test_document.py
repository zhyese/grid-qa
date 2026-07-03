"""文档处理纯函数单测：扩展名路由、上传校验常量。"""
import asyncio

from app.services.document_service import _ext, ALLOWED_EXT, MAX_FILES, MAX_SINGLE_SIZE


def test_ext_lowercase():
    assert _ext("report.PDF") == ".pdf"
    assert _ext("doc.docx") == ".docx"
    assert _ext("img.JPEG") == ".jpeg"


def test_ext_no_dot():
    assert _ext("noextension") == ""


def test_ext_multi_dot():
    assert _ext("archive.tar.gz") == ".gz"


def test_allowed_ext_covers_common():
    for e in [".pdf", ".docx", ".txt", ".md", ".png", ".jpg"]:
        assert e in ALLOWED_EXT


def test_upload_limits():
    assert MAX_FILES == 5
    assert MAX_SINGLE_SIZE == 100 * 1024 * 1024  # 100MB


def test_vectorize_documents_collects_success_and_fail(monkeypatch):
    """批量向量化：单个文档失败不中断，正确分类 successList/failList。"""
    from app.services import document_service

    async def fake_vectorize(db, doc_id):
        if doc_id.startswith("bad"):
            raise RuntimeError("未解析")
        return {"docId": doc_id, "vectorCount": 3, "milvusCollection": "c", "embeddingRoute": "bge", "docChars": 9}

    monkeypatch.setattr(document_service, "vectorize_document", fake_vectorize)
    result = asyncio.run(document_service.vectorize_documents(None, ["ok1", "bad1", "ok2"]))

    assert len(result["successList"]) == 2
    assert result["successList"][0]["docId"] == "ok1"
    assert result["successList"][1]["docId"] == "ok2"
    assert len(result["failList"]) == 1
    assert "bad1" in result["failList"][0]
    assert "未解析" in result["failList"][0]


def test_vectorize_documents_empty_input(monkeypatch):
    """空入参返回空 successList/failList，不报错（边界）。"""
    from app.services import document_service

    result = asyncio.run(document_service.vectorize_documents(None, []))
    assert result == {"successList": [], "failList": []}
