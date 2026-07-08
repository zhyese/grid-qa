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


class PersonaConfigRequest(BaseModel):
    name: str                            # S5：persona 名（diagnose/qa/alert/自定义）
    systemPrompt: str = ""
    allowedTools: str = ""               # JSON 数组字符串，如 '["search_regulation"]'
    maxIter: Optional[int] = None
    temperature: Optional[float] = None
    maxTokens: Optional[int] = None
    outputFormat: Optional[str] = None   # json | text
    enabled: bool = True


class AiDraftUpdateRequest(BaseModel):
    aiDraft: str = ""                    # 就地编辑保存 AI 草稿（点击文本直接改）


class ConfidenceUpdateRequest(BaseModel):
    confidence: str = ""                 # 后台标注 confidence（medium/refused/sufficient/...）
