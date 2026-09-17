"""有界输入；时间统一要求显式时区。"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TimeRange(StrictBody):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("时间必须包含时区，例如 +08:00")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def ordered(self):
        if not 0 < (self.end - self.start).total_seconds() <= 31 * 86400:
            raise ValueError("时间范围必须大于零且不超过31天")
        return self


class ImpactRequest(StrictBody):
    old_doc_id: str = Field(min_length=1, max_length=64)
    new_doc_id: str = Field(min_length=1, max_length=64)


class HandoverRequest(TimeRange):
    title: str = Field(min_length=1, max_length=200)


class ReviewRequest(StrictBody):
    version: int = Field(ge=1)
    action: Literal["review", "accept"]
    note: str = Field(min_length=1, max_length=2000)


class PointInput(StrictBody):
    source: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=64)
    unit: str = Field(min_length=1, max_length=24)
    value: float = Field(allow_inf_nan=False)
    quality: Literal["good", "bad", "uncertain"]
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("采样时间必须包含时区")
        return value.astimezone(timezone.utc)


class ImportRequest(StrictBody):
    points: list[PointInput] = Field(min_length=1, max_length=2000)


class TelemetryQuery(TimeRange):
    device_id: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=64)


class TableQuery(StrictBody):
    doc_id: str = Field(min_length=1, max_length=64)
    column: str = Field(min_length=1, max_length=200)
    filters: dict[str, str] = Field(min_length=1, max_length=10)

    @field_validator("filters")
    @classmethod
    def bounded(cls, value):
        if any(
            not k.strip() or not v.strip() or len(k) > 200 or len(v) > 200
            for k, v in value.items()
        ):
            raise ValueError("条件名称和值须非空且不超过200字符")
        return {k.strip(): v.strip() for k, v in value.items()}
