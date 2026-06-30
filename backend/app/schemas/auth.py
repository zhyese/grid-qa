"""认证相关 schema。"""
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    role: str = Field(default="operator", pattern="^(admin|operator)$")
    tenantId: str = Field(default="default", max_length=64)  # 多租户：注册时绑定租户


class TokenData(BaseModel):
    token: str
    username: str
    role: str
