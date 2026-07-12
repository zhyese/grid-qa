"""检索/文档级 ACL 过滤单测（_acl_ok 后置过滤 + _assert_acl 逐文档校验）。

项目惯例：纯函数测，无需 DB/pytest-asyncio。Document 用轻量替身避免连库。
"""
import pytest

from app.services.retrieval_service import _acl_ok
from app.services.document_service import _assert_acl
from app.core.response import BizError


class _Doc:
    """轻量 Document 替身（只关心 dept/allowed_roles 两个字段）。"""
    def __init__(self, dept="", allowed_roles=""):
        self.dept = dept
        self.allowed_roles = allowed_roles


# ===== _acl_ok：检索后置过滤（返回 bool，不抛）=====

def test_acl_no_context_passthrough():
    """未传 user 上下文 = 不过滤（向后兼容：admin 链路/旧调用）。"""
    assert _acl_ok("", "", None, None)
    assert _acl_ok("检修", "editor", None, None)


def test_acl_public_doc():
    """文档 dept 空 = 公开，任何用户可读。"""
    assert _acl_ok("", "", "调度", "operator")
    assert _acl_ok("", "editor", "检修", "operator")


def test_acl_same_dept_pass():
    assert _acl_ok("调度", "", "调度", "operator")
    assert _acl_ok("调度", "operator,editor", "调度", "operator")


def test_acl_cross_dept_denied():
    assert not _acl_ok("检修", "", "调度", "operator")
    assert not _acl_ok("调度", "editor", "检修", "operator")


def test_acl_admin_bypass():
    """admin 跨部门/跨角色也放行。"""
    assert _acl_ok("检修", "editor", "调度", "admin")


def test_acl_role_not_allowed():
    """同 dept 但 allowed_roles 不含用户角色 = 拒。"""
    assert not _acl_ok("调度", "editor", "调度", "operator")
    assert not _acl_ok("调度", "auditor", "调度", "operator")


# ===== _assert_acl：逐文档校验（越权抛 BizError 403）=====

def test_assert_acl_raises_cross_dept():
    doc = _Doc(dept="检修", allowed_roles="")
    with pytest.raises(BizError):
        _assert_acl(doc, "调度", "operator")


def test_assert_acl_raises_role_unauthorized():
    doc = _Doc(dept="调度", allowed_roles="editor")
    with pytest.raises(BizError):
        _assert_acl(doc, "调度", "operator")


def test_assert_acl_admin_pass():
    _assert_acl(_Doc(dept="检修", allowed_roles="editor"), "调度", "admin")  # 不抛


def test_assert_acl_none_context_skip():
    """向后兼容：无 user 上下文直接放行。"""
    _assert_acl(_Doc(dept="检修", allowed_roles="editor"), None, None)


def test_assert_acl_public_and_same_dept_pass():
    _assert_acl(_Doc(dept="", allowed_roles=""), "调度", "operator")  # 公开
    _assert_acl(_Doc(dept="调度", allowed_roles=""), "调度", "operator")  # 同dept全员
    _assert_acl(_Doc(dept="调度", allowed_roles="operator,editor"), "调度", "operator")  # 角色命中
