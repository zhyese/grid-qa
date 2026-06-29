"""检索相关 schema。"""
from pydantic import BaseModel, Field


class MixedRetrievalRequest(BaseModel):
    query: str
    topK: int = Field(default=10, ge=1, le=50)
