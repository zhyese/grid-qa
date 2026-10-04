"""F1 数据资产与标注工作台请求模型。"""
from typing import Optional

from pydantic import BaseModel, Field


class GoldenCaseRequest(BaseModel):
    query: str = Field("", max_length=2000)
    expect: list[str] = Field(default_factory=list)
    relevantDocs: dict[str, int] = Field(default_factory=dict)
    category: str = Field("", max_length=32)
    difficulty: str = Field("", max_length=16)          # easy | medium | hard
    deviceTags: list[str] = Field(default_factory=list)
    expectedSources: list[str] = Field(default_factory=list)
    labelStatus: str = Field("pending", max_length=16)  # pending|labeled|verified|rejected
    source: str = Field("manual", max_length=32)
    note: str = Field("", max_length=500)


class GoldenPublishRequest(BaseModel):
    note: str = Field("", max_length=500)


class GoldenIngestRequest(BaseModel):
    limit: int = Field(200, ge=1, le=1000)


class GoldenExportQuery(BaseModel):
    labelStatus: Optional[str] = None
