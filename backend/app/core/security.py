"""密码哈希（bcrypt 直连）与 JWT 签发/校验。

注：不使用 passlib，因其依赖 bcrypt.__about__ 在 bcrypt>=4.1 已移除，会报 AttributeError。
"""
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from jwt import ExpiredSignatureError, InvalidTokenError

from app.config import settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: str, username: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload = {"sub": user_id, "username": username, "role": role, "exp": expire}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """解码 token；失败抛 InvalidTokenError/ExpiredSignatureError，由调用方转业务异常。"""
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
