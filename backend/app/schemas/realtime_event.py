"""实时事件接入与主动运维闭环的请求模型。"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


EventSource = Literal["scada", "oms", "pms", "generic"]


class RealtimeDeviceRef(BaseModel):
    """外部系统中的设备引用；规范设备身份由映射表补齐。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    sourceDeviceId: str = Field(default="", max_length=128)
    name: str = Field(default="", max_length=200)
    type: str = Field(default="", max_length=64)
    station: str = Field(default="", max_length=200)


class RealtimeEventIn(BaseModel):
    """统一事件信封。

    ``payload`` 保留源系统原始字段，公共字段用于跨 SCADA/OMS/PMS 的稳定编排。
    未识别字段也会保留，以便连接器渐进迁移到统一信封。
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    eventId: str = Field(min_length=1, max_length=128)
    source: EventSource
    eventType: str = Field(default="alarm", max_length=64)
    severity: str = Field(default="warning", max_length=32)
    occurredAt: datetime | None = None
    title: str = Field(default="", max_length=256)
    summary: str = Field(default="", max_length=4000)
    device: RealtimeDeviceRef | None = None
    measurements: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    modelType: str | None = Field(default=None, max_length=64)

    @field_validator("eventId", "eventType", mode="before")
    @classmethod
    def _strip_required_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("source", mode="before")
    @classmethod
    def _normalize_source(cls, value: Any) -> str:
        return str(value or "").strip().lower()


class DeviceMappingUpsertRequest(BaseModel):
    source: EventSource
    sourceDeviceId: str = Field(min_length=1, max_length=128)
    canonicalDeviceId: str = Field(min_length=1, max_length=128)
    canonicalName: str = Field(default="", max_length=200)
    deviceType: str = Field(default="", max_length=64)
    station: str = Field(default="", max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)
    active: bool = True

    @field_validator("source", mode="before")
    @classmethod
    def _normalize_source(cls, value: Any) -> str:
        return str(value or "").strip().lower()

    @field_validator("sourceDeviceId", "canonicalDeviceId", mode="before")
    @classmethod
    def _strip_id(cls, value: Any) -> str:
        return str(value or "").strip()


class RunReviewRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class RunRetryRequest(BaseModel):
    modelType: str | None = Field(default=None, max_length=64)

