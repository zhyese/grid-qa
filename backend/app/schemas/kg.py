"""知识图谱相关 schema。"""
from typing import Optional, Literal

from pydantic import BaseModel, Field, field_validator


class KgExtractRequest(BaseModel):
    docId: str
    modelType: Optional[Literal['deepseek', 'qwen', 'doubao']] = None


class KgBatchExtractRequest(BaseModel):
    docIds: list[str] = Field(min_length=1, max_length=200)
    modelType: Optional[Literal['deepseek', 'qwen', 'doubao']] = None

    @field_validator('docIds')
    @classmethod
    def valid_ids(cls, values):
        if any(not value.strip() or len(value) > 64 for value in values):
            raise ValueError('文档 ID 不能为空且不得超过 64 字符')
        return list(dict.fromkeys(value.strip() for value in values))
