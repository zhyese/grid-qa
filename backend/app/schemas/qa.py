"""问答相关 schema。"""
from typing import List, Optional

from pydantic import BaseModel


class QaAnswerRequest(BaseModel):
    query: str
    modelType: Optional[str] = None  # deepseek | qwen | doubao；空则用 LLM_PROVIDER


class QaAnswerData(BaseModel):
    answer: str
    retrievalSource: List[str] = []
    responseTime: float = 0.0
    hallucinationRate: float = 0.0


class TermRequest(BaseModel):
    term: str
