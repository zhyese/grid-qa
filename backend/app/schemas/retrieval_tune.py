"""检索调参报告 schema。"""
from pydantic import BaseModel


class TuneSuggestion(BaseModel):
    param: str
    current: float | int | bool | None = None
    suggested: float | int | bool
    metric: str
    delta: float
    confidence: str  # high / medium / low
    reason: str
