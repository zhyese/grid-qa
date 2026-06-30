"""文档处理纯函数单测：扩展名路由、上传校验常量。"""
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
