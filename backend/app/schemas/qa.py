"""问答相关 schema。"""
from typing import List, Optional

from pydantic import BaseModel


class QaAnswerRequest(BaseModel):
    query: str
    modelType: Optional[str] = None       # deepseek | qwen | doubao
    conversationId: Optional[str] = None  # 多轮对话 id（首次不传则新建）


class QaAnswerData(BaseModel):
    answer: str
    retrievalSource: List[str] = []
    responseTime: float = 0.0
    hallucinationRate: float = 0.0
    cached: bool = False
    conversationId: str = ""


class TermRequest(BaseModel):
    term: str
