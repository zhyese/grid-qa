"""P1-8 RBAC DB 覆盖（role_permission 表）单测。

覆盖语义：覆盖优先于 code 默认（True 补权 / False 回收）；admin 不受覆盖；
缓存写入即失效；非法 role/permission 拒绝。
"""
import pytest

from app.core.permissions import has_perm
from app.services import role_permission_service as rps


@pytest.fixture(autouse=True)
def _reset_cache():
    rps._invalidate()
    yield
    rps._invalidate()


@pytest.mark.asyncio
async def test_default_without_override(test_db):
    """无覆盖行 → 走 code 默认：operator 无 kg:edit。"""
    assert has_perm("operator", "kg:edit") is False
    assert await rps.effective_override("operator", "kg:edit", db=test_db) is None
    assert await rps.effective_override("admin", "kg:edit", db=test_db) is None  # admin 不入表


@pytest.mark.asyncio
async def test_grant_override_adds_permission(test_db):
    """granted=True 补权：operator + kg:edit → 有效。"""
    await rps.set_override(test_db, "operator", "kg:edit", True)
    assert await rps.effective_override("operator", "kg:edit", db=test_db) is True
    assert await rps.effective_override("auditor", "kg:edit", db=test_db) is None  # 只影响目标角色


@pytest.mark.asyncio
async def test_deny_override_revokes_permission(test_db):
    """granted=False 回收：editor 的 doc:read 被撤销。"""
    assert has_perm("editor", "doc:read") is True
    await rps.set_override(test_db, "editor", "doc:read", False)
    assert await rps.effective_override("editor", "doc:read", db=test_db) is False


@pytest.mark.asyncio
async def test_set_override_upsert_idempotent(test_db):
    """同 (role, permission) 反复写 → 单行更新，不重复插入。"""
    await rps.set_override(test_db, "operator", "report:manage", True)
    await rps.set_override(test_db, "operator", "report:manage", False)
    rows = await rps.list_overrides(test_db)
    target = [r for r in rows if r["role"] == "operator" and r["permission"] == "report:manage"]
    assert len(target) == 1
    assert target[0]["granted"] is False


@pytest.mark.asyncio
async def test_set_override_rejects_admin_and_unknown(test_db):
    """admin 拒绝覆盖；未知角色/权限拒绝。"""
    from app.core.response import BizError
    with pytest.raises(BizError):
        await rps.set_override(test_db, "admin", "kg:edit", False)
    with pytest.raises(BizError):
        await rps.set_override(test_db, "superuser", "kg:edit", True)
    with pytest.raises(BizError):
        await rps.set_override(test_db, "editor", "not:a:perm", True)


@pytest.mark.asyncio
async def test_effective_catalog_merges_overrides(test_db):
    """目录：默认集 + 覆盖合并后的生效态。"""
    await rps.set_override(test_db, "operator", "kg:edit", True)
    await rps.set_override(test_db, "editor", "doc:read", False)
    cat = await rps.effective_catalog(test_db)
    assert "kg:edit" in cat["roles"]["operator"]["effective"]
    assert "doc:read" not in cat["roles"]["editor"]["effective"]
    assert cat["roles"]["editor"]["overrides"]["doc:read"] is False
    assert cat["roles"]["admin"]["effective"] == ["*"]
