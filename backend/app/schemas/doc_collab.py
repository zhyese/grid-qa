"""文档协作批注 + 电子签批 API 入参 schema。"""
from pydantic import BaseModel, Field


class AnnotationCreate(BaseModel):
    chunkIdx: int = Field(0, ge=0)
    quote: str = Field("", max_length=500)
    content: str = Field(..., min_length=1, max_length=2000)


class AnnotationReply(BaseModel):
    content: str = Field(..., min_length=1, max_length=500)


class AnnotationResolve(BaseModel):
    resolved: bool = True


class SignoffNodeIn(BaseModel):
    signer: str = Field(..., max_length=64)
    role: str = Field("", max_length=16)


class SignoffCreate(BaseModel):
    title: str = Field("", max_length=200)
    signers: list[SignoffNodeIn] = Field(..., min_length=1, max_length=10)


class SignoffAction(BaseModel):
    """签批/驳回：电子签批要求口令复核（不信任纯会话态）。"""
    password: str = Field(..., min_length=1, max_length=128)
    comment: str = Field("", max_length=500)
    reason: str = Field("", max_length=500)  # 驳回原因（复用同一 schema）
