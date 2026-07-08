"""系统（日志/配置）相关 schema。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class LogItem(BaseModel):
    id: str
    operateUser: str
    operateType: str
    operateTime: str
    content: str


class LogQuery(BaseModel):
    page: int = 1
    size: int = 10
    startTime: Optional[str] = None
    endTime: Optional[str] = None


class MilvusConfigRequest(BaseModel):
    indexType: str
    param: dict = {}


class ModelConfigRequest(BaseModel):
    modelType: str
    param: dict = {}


class AlertDisposeRequest(BaseModel):
    severity: str = "warning"            # S3：手动触发告警处置
    title: str = ""
    summary: str = ""
    modelType: Optional[str] = None
