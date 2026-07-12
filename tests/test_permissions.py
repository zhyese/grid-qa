"""RBAC 权限矩阵单测（has_perm + 角色-权限默认映射）。

项目惯例：纯函数测，无需 DB/pytest-asyncio（参考 tests/test_agent_runtime.py）。
设计对齐 docs/superpowers/specs/2026-07-01-rbac-acl-design.md。
"""
from app.core.permissions import has_perm, role_perms, VALID_ROLES, ROLE_PERMISSIONS


def test_admin_all_pass():
    """admin 对任意权限放行（含通配与未知 perm）。"""
    for p in ["doc:delete", "system:config", "user:manage", "anything:weird"]:
        assert has_perm("admin", p), f"admin 应放行 {p}"


def test_editor_doc_full_but_no_admin():
    """editor 文档全权 + 问答/图谱编辑/领域，但无 system:config/user:manage。"""
    assert has_perm("editor", "doc:upload")
    assert has_perm("editor", "doc:delete")
    assert has_perm("editor", "doc:manage")
    assert has_perm("editor", "qa:answer")
    assert has_perm("editor", "kg:edit")
    assert has_perm("editor", "domain:use")
    assert not has_perm("editor", "system:config")
    assert not has_perm("editor", "user:manage")
    assert not has_perm("editor", "alert:manage")


def test_operator_no_dangerous():
    """operator 问答/读文档/读图谱/领域，但不能删/传文档、系统配置、用户管理。"""
    assert has_perm("operator", "qa:answer")
    assert has_perm("operator", "doc:read")
    assert has_perm("operator", "kg:read")
    assert not has_perm("operator", "doc:delete")
    assert not has_perm("operator", "doc:upload")
    assert not has_perm("operator", "doc:manage")
    assert not has_perm("operator", "kg:edit")
    assert not has_perm("operator", "system:config")


def test_auditor_readonly():
    """auditor 全只读（含审计/告警/指标读），无任何写/编辑/管理。"""
    assert has_perm("auditor", "doc:read")
    assert has_perm("auditor", "qa:answer")
    assert has_perm("auditor", "audit:read")
    assert has_perm("auditor", "alert:read")
    assert has_perm("auditor", "metric:read")
    assert not has_perm("auditor", "kg:edit")
    assert not has_perm("auditor", "doc:delete")
    assert not has_perm("auditor", "doc:upload")
    assert not has_perm("auditor", "system:config")


def test_unknown_role_denied():
    """未知角色无任何权限。"""
    assert not has_perm("guest", "doc:read")
    assert not has_perm("superuser", "system:config")
    assert role_perms("ghost") == set()


def test_valid_roles_complete():
    """4 角色齐备（admin/editor/operator/auditor）。"""
    assert VALID_ROLES == {"admin", "editor", "operator", "auditor"}
    for r in VALID_ROLES:
        assert r in ROLE_PERMISSIONS
