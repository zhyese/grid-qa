"""FastAPI 依赖：数据库会话、当前用户解析、管理员校验。"""
from typing import Optional

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import SYSTEM_CONFIG, has_perm
from app.core.response import BizError
from app.core.security import decode_token
from app.db.session import get_db
from app.models.user import User
from app.services.auth_service import get_user_by_id


async def get_current_user(
    authorization: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise BizError("未登录或缺少认证 token", 401)
    token = authorization[len("Bearer ") :]
    try:
        payload = decode_token(token)
    except Exception:
        raise BizError("token 无效或已过期", 401)
    user = await get_user_by_id(db, payload.get("sub", ""))
    if not user:
        raise BizError("用户不存在或已被删除", 401)
    return user


def require_perm(perm: str):
    """RBAC 权限依赖工厂：require_perm('doc:delete') → 仅拥有该权限的角色可访问。

    admin 全放行；其余查 has_perm（ROLE_PERMISSIONS 默认 + role_permission 表覆盖，后续）。
    用法：`Depends(require_perm("doc:delete"))`。
    """
    async def _check(user: User = Depends(get_current_user)) -> User:
        if not has_perm(user.role, perm):
            raise BizError(f"无权限：需要 {perm}", 403)
        return user
    return _check


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """兼容别名：等同 require_perm('system:config')。仅 admin 拥有 system:config。"""
    if not has_perm(user.role, SYSTEM_CONFIG):
        raise BizError("需要管理员权限", 403)
    return user
