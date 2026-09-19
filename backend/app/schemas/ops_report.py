"""N7 运维报告 API 入参 schema。"""
from pydantic import BaseModel, Field


class ReportCreate(BaseModel):
    reportType: str = Field("shift", description="shift|weekly|fault|device")
    days: int | None = Field(None, ge=1, le=366, description="统计窗口天数，缺省按类型默认")
    deviceId: str = Field("", max_length=128, description="fault/device 报告的聚焦设备")
    title: str = Field("", max_length=120)
    modelType: str | None = None
