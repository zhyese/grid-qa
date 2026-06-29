"""FastAPI 依赖：数据库会话、当前用户解析、管理员校验。"""
from typing import Optional

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

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


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise BizError("需要管理员权限", 403)
    return user
