"""认证服务：登录校验、注册、按 id 查用户。"""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import BizError
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User


async def authenticate(db: AsyncSession, username: str, password: str) -> dict:
    user = await get_user_by_username(db, username)
    if not user or not verify_password(password, user.password_hash):
        raise BizError("用户名或密码错误", 401)
    token = create_access_token(user.id, user.username, user.role)
    return {"token": token, "username": user.username, "role": user.role}


async def register_user(db: AsyncSession, username: str, password: str, role: str = "operator") -> dict:
    if await get_user_by_username(db, username):
        raise BizError("用户名已存在", 400)
    user = User(username=username, password_hash=hash_password(password), role=role)
    db.add(user)
    await db.commit()
    return {"userId": user.id, "username": user.username}


async def get_user_by_username(db: AsyncSession, username: str) -> Optional[User]:
    res = await db.execute(select(User).where(User.username == username))
    return res.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: str) -> Optional[User]:
    res = await db.execute(select(User).where(User.id == user_id))
    return res.scalar_one_or_none()
