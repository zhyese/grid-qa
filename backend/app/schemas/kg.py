"""知识图谱相关 schema。"""
from typing import Optional

from pydantic import BaseModel


class KgExtractRequest(BaseModel):
    docId: str
    modelType: Optional[str] = None       # deepseek | qwen | doubao
