"""RBAC 角色-权限 DB 覆盖服务（P1-8 收口：role_permission 表落地）。

code 默认映射（app.core.permissions.ROLE_PERMISSIONS，首版硬编码）之外，
支持按 (role, permission) 粒度的 DB 覆盖：granted=True 授予（补权）、False 撤销（回收）。
admin 角色不入表（全权是代码红线，见 core/permissions.py）。

合并语义（dependencies.require_perm 消费）：
- 覆盖优先于 code 默认：denied（False）回收比授予更优先；
- 无覆盖行 → 走 code 默认。
进程内 TTL 缓存（60s）避免每请求查表；写入即失效，权限变更最迟 60s 生效（跨进程）。
"""
import time

from sqlalchemy import select

from app.core.permissions import ROLE_PERMISSIONS, VALID_ROLES
from app.core.response import BizError
from app.models.permission import RolePermission

_CACHE_TTL_SECONDS = 60
_cache: dict = {"loaded_at": 0.0, "data": {}}  # data: {role: {permission: granted}}

# 权限目录（UI 编辑页渲染用；与 core/permissions.py 常量一一对应）
PERMISSION_CATALOG = sorted({p for perms in ROLE_PERMISSIONS.values() for p in perms if p != "*"})


def _invalidate() -> None:
    _cache["loaded_at"] = 0.0


async def _load_overrides(db=None) -> dict[str, dict[str, bool]]:
    """加载全部覆盖行（带 TTL 缓存）。测试可直接传 db 走注入会话。"""
    now = time.time()
    if db is None and now - _cache["loaded_at"] < _CACHE_TTL_SECONDS:
        return _cache["data"]
    try:
        if db is None:
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(select(RolePermission))).scalars().all()
        else:
            rows = (await db.execute(select(RolePermission))).scalars().all()
        data: dict[str, dict[str, bool]] = {}
        for r in rows:
            data.setdefault(r.role, {})[r.permission] = bool(r.granted)
        if db is None:
            _cache["data"] = data
            _cache["loaded_at"] = now
        return data
    except Exception:
        # DB 不可达 → 视为无覆盖（degrade 到 code 默认，绝不因覆盖层挂掉而全站 403）
        from app.core.obs import degraded
        degraded("role_perm_override_load", Exception("role_permission load failed"))
        return _cache["data"] if db is None else {}


async def effective_override(role: str, permission: str, db=None) -> bool | None:
    """(role, permission) 的 DB 覆盖值；无覆盖返回 None（走 code 默认）。

    db=None（生产路径 require_perm）：走 TTL 缓存 + 独立会话；测试可注入会话绕开缓存。
    """
    if role == "admin":
        return None  # admin 全权不入表、不受回收
    data = await _load_overrides(db)
    return data.get(role, {}).get(permission)


async def list_overrides(db) -> list[dict]:
    rows = (await db.execute(select(RolePermission).order_by(RolePermission.role, RolePermission.permission))).scalars().all()
    return [
        {"role": r.role, "permission": r.permission, "granted": bool(r.granted), "createdAt": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ]


async def set_override(db, role: str, permission: str, granted: bool) -> dict:
    """upsert 一条覆盖（同 role+permission 唯一）。写后失效缓存。"""
    if role not in VALID_ROLES or role == "admin":
        raise BizError(f"非法角色：{role}（admin 全权不受覆盖）", 400)
    if permission not in PERMISSION_CATALOG:
        raise BizError(f"未知权限：{permission}", 400)
    row = (await db.execute(
        select(RolePermission).where(RolePermission.role == role, RolePermission.permission == permission)
    )).scalar_one_or_none()
    if row:
        row.granted = bool(granted)
    else:
        row = RolePermission(role=role, permission=permission, granted=bool(granted))
        db.add(row)
    await db.commit()
    _invalidate()
    return {"role": role, "permission": permission, "granted": bool(granted)}


async def effective_catalog(db) -> dict:
    """UI 用：权限目录 + 各角色默认集 + 生效态（默认 XOR/覆盖合并后）+ 覆盖明细。"""
    data = await _load_overrides(db)
    roles = {}
    for role, perms in ROLE_PERMISSIONS.items():
        if role == "admin":
            roles[role] = {"default": ["*"], "effective": ["*"], "overrides": {}}
            continue
        eff = {}
        for p in PERMISSION_CATALOG:
            ov = data.get(role, {}).get(p)
            eff[p] = bool(p in perms) if ov is None else bool(ov)
        roles[role] = {
            "default": sorted(perms),
            "effective": sorted(p for p, ok in eff.items() if ok),
            "overrides": {p: v for p, v in data.get(role, {}).items() if p in PERMISSION_CATALOG},
        }
    return {"permissions": PERMISSION_CATALOG, "roles": roles, "overrides": await list_overrides(db)}
