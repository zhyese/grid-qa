"""认证相关 schema。"""
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    role: str = Field(default="operator", pattern="^(admin|operator)$")


class TokenData(BaseModel):
    token: str
    username: str
    role: str
