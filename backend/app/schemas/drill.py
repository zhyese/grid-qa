"""N5 故障仿真演练沙箱 API 入参 schema。"""
from pydantic import BaseModel, Field


class PropagationEvent(BaseModel):
    tOffset: int = Field(0, ge=0, le=86400, description="事件发生偏移（秒，相对演练开始）")
    device: str = Field("", max_length=128)
    event: str = Field(..., min_length=1, max_length=300)
    severity: str = Field("warning", description="critical|warning|info")


class ChecklistItem(BaseModel):
    id: str = Field("", max_length=16)
    action: str = Field(..., min_length=1, max_length=200)
    keywords: list[str] = Field(default_factory=list, max_length=8)


class ScenarioCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=500)
    stationId: str = Field("", max_length=64)
    faultDevice: str = Field("", max_length=128)
    faultDesc: str = Field("", max_length=500)
    propagation: list[PropagationEvent]
    checklist: list[ChecklistItem]
    difficulty: str = Field("normal")


class ScenarioUpdate(BaseModel):
    name: str | None = Field(None, max_length=120)
    description: str | None = Field(None, max_length=500)
    stationId: str | None = Field(None, max_length=64)
    faultDevice: str | None = Field(None, max_length=128)
    faultDesc: str | None = Field(None, max_length=500)
    propagation: list[PropagationEvent] | None = None
    checklist: list[ChecklistItem] | None = None
    difficulty: str | None = Field(None)
    enabled: bool | None = None


class FaultChainGen(BaseModel):
    stationId: str = Field("", max_length=64)
    deviceId: str = Field(..., min_length=1, max_length=128)
    name: str = Field("", max_length=120)


class RunAction(BaseModel):
    action: str = Field(..., min_length=1, max_length=300)


class RunStart(BaseModel):
    scenarioId: str = Field(..., min_length=1, max_length=64)
