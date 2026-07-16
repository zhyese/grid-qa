"""知识自进化闭环请求/响应 schema。"""
from pydantic import BaseModel, Field


class EvolutionScanRequest(BaseModel):
    sinceHours: int = Field(default=168, ge=1, le=2160, description="回溯多少小时的 dislike")
    modelType: str | None = None


class DraftReviewRequest(BaseModel):
    action: str = Field(..., pattern="^(approve|reject)$")
    note: str = Field(default="", max_length=500)


class DraftWithdrawRequest(BaseModel):
    note: str = Field(default="", max_length=500)
