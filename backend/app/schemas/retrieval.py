"""检索相关 schema。"""
from typing import Optional

from pydantic import BaseModel, Field


class MixedRetrievalRequest(BaseModel):
    query: str
    topK: int = Field(default=10, ge=1, le=50)
    docType: Optional[str] = None    # 按文档类型过滤（运维手册/故障案例/...）
    modelType: Optional[str] = None  # query 改写用的 LLM（可选）
    equipment: Optional[str] = None  # 按设备台账标签过滤（D5，精确到具体设备）
