"""认证服务：登录校验、注册、按 id 查用户。"""
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import BizError
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User


async def authenticate(db: AsyncSession, username: str, password: str) -> dict:
    user = await get_user_by_username(db, username)
    if not user or not verify_password(password, user.password_hash):
        raise BizError("用户名或密码错误", 401)
    if getattr(user, "status", "active") != "active":
        raise BizError("账号已被禁用，请联系管理员", 403)
    token = create_access_token(user.id, user.username, user.role)
    return {"token": token, "username": user.username, "role": user.role}


async def register_user(
    db: AsyncSession, username: str, password: str, role: str = "operator",
    tenant_id: str = "default", dept: str = "",
) -> dict:
    if await get_user_by_username(db, username):
        raise BizError("用户名已存在", 400)
    user = User(
        username=username, password_hash=hash_password(password),
        role=role, tenant_id=tenant_id or "default", dept=dept,
    )
    db.add(user)
    await db.commit()
    return {"userId": user.id, "username": user.username, "tenantId": user.tenant_id, "dept": user.dept}


async def get_user_by_username(db: AsyncSession, username: str) -> Optional[User]:
    res = await db.execute(select(User).where(User.username == username))
    return res.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: str) -> Optional[User]:
    res = await db.execute(select(User).where(User.id == user_id))
    return res.scalar_one_or_none()


async def list_users(db: AsyncSession, page: int = 1, size: int = 50,
                     tenant_id: str | None = None) -> dict:
    """用户列表（admin 用户管理用）。可按 tenant 过滤，分页。"""
    stmt = select(User)
    cnt = select(func.count()).select_from(User)
    if tenant_id:
        stmt = stmt.where(User.tenant_id == tenant_id)
        cnt = cnt.where(User.tenant_id == tenant_id)
    total = (await db.execute(cnt)).scalar() or 0
    rows = (await db.execute(
        stmt.order_by(User.created_at.desc()).offset((page - 1) * size).limit(size)
    )).scalars().all()
    return {
        "total": total,
        "list": [
            {"userId": r.id, "username": r.username, "role": r.role,
             "dept": r.dept or "", "tenantId": r.tenant_id,
             "status": getattr(r, "status", "active") or "active",
             "createdAt": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""}
            for r in rows
        ],
    }


async def update_user_role(db: AsyncSession, user_id: str, role: str,
                           dept: str | None = None) -> dict:
    """改用户角色/dept（admin 用户管理）。role 必须在 VALID_ROLES 内。"""
    from app.core.permissions import VALID_ROLES
    if role not in VALID_ROLES:
        raise BizError(f"非法角色：{role}（合法：{sorted(VALID_ROLES)}）", 400)
    user = await get_user_by_id(db, user_id)
    if not user:
        raise BizError("用户不存在", 404)
    user.role = role
    if dept is not None:
        user.dept = dept
    await db.commit()
    return {"userId": user.id, "username": user.username, "role": user.role, "dept": user.dept}


async def _count_active_admins(db: AsyncSession) -> int:
    """当前 active 状态的 admin 数量（防最后一个管理员被禁删锁死）。"""
    return (await db.execute(
        select(func.count()).select_from(User).where(User.role == "admin", User.status == "active")
    )).scalar() or 0


async def set_user_status(db: AsyncSession, user_id: str, status: str, actor_id: str) -> dict:
    """启用/禁用账号（admin）。不能操作自己；不能禁用最后一个 active admin。"""
    if status not in ("active", "inactive"):
        raise BizError("非法 status：仅 active|inactive", 400)
    user = await get_user_by_id(db, user_id)
    if not user:
        raise BizError("用户不存在", 404)
    if user_id == actor_id:
        raise BizError("不能禁用/启用自己", 400)
    if user.role == "admin" and status == "inactive" and await _count_active_admins(db) <= 1:
        raise BizError("不能禁用最后一个管理员（避免系统锁死）", 400)
    user.status = status
    await db.commit()
    return {"userId": user.id, "username": user.username, "status": user.status}


async def delete_user(db: AsyncSession, user_id: str, actor_id: str) -> dict:
    """删除账号（admin）。不能删自己；不能删最后一个 active admin。"""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise BizError("用户不存在", 404)
    if user_id == actor_id:
        raise BizError("不能删除自己", 400)
    if user.role == "admin" and await _count_active_admins(db) <= 1:
        raise BizError("不能删除最后一个管理员（避免系统锁死）", 400)
    await db.delete(user)
    await db.commit()
    return {"userId": user_id, "deleted": True}


async def reset_password(db: AsyncSession, user_id: str, new_password: str) -> dict:
    """管理员重置用户密码。"""
    if not new_password or len(new_password) < 6:
        raise BizError("密码至少 6 位", 400)
    user = await get_user_by_id(db, user_id)
    if not user:
        raise BizError("用户不存在", 404)
    user.password_hash = hash_password(new_password)
    await db.commit()
    return {"userId": user.id, "reset": True}


async def get_profile(db: AsyncSession, user_id: str) -> dict:
    """用户自助：查自己的资料。"""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise BizError("用户不存在", 404)
    return {"userId": user.id, "username": user.username, "role": user.role,
            "dept": user.dept or "", "tenantId": user.tenant_id, "status": getattr(user, "status", "active"),
            "createdAt": user.created_at.strftime("%Y-%m-%d %H:%M:%S") if user.created_at else ""}


async def update_profile(db: AsyncSession, user_id: str, dept: str | None = None) -> dict:
    """用户自助：改自己的部门（影响文档级 ACL）。角色/租户由管理员管理，不在此改。"""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise BizError("用户不存在", 404)
    if dept is not None:
        user.dept = dept.strip()[:64]
    await db.commit()
    return {"userId": user.id, "username": user.username, "dept": user.dept}


async def change_password(db: AsyncSession, user_id: str, old_password: str, new_password: str) -> dict:
    """用户自助改密码：必须校验旧密码。"""
    if not new_password or len(new_password) < 6:
        raise BizError("新密码至少 6 位", 400)
    user = await get_user_by_id(db, user_id)
    if not user:
        raise BizError("用户不存在", 404)
    if not verify_password(old_password, user.password_hash):
        raise BizError("旧密码错误", 400)
    user.password_hash = hash_password(new_password)
    await db.commit()
    return {"userId": user.id, "changed": True}
