"""认证相关 schema。"""
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    role: str = Field(default="operator", pattern="^(admin|editor|operator|auditor)$")
    tenantId: str = Field(default="default", max_length=64)  # 多租户：注册时绑定租户
    dept: str = Field(default="", max_length=64)  # 部门，文档级 ACL 用


class TokenData(BaseModel):
    token: str
    username: str
    role: str


class UpdateRoleRequest(BaseModel):
    role: str
    dept: str = ""


class UserStatusRequest(BaseModel):
    status: str = Field(pattern="^(active|inactive)$")  # 启用|禁用


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=6, max_length=64)


class ProfileUpdateRequest(BaseModel):
    dept: str = Field(default="", max_length=64)


class ChangePasswordRequest(BaseModel):
    oldPassword: str
    newPassword: str = Field(min_length=6, max_length=64)
