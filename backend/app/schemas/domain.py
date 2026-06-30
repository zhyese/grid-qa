"""领域增强 schema：故障诊断 / 相似案例 / 两票生成。"""
from typing import Optional

from pydantic import BaseModel


class DiagnoseRequest(BaseModel):
    symptom: str                     # 故障症状描述
    modelType: Optional[str] = None


class SimilarCaseRequest(BaseModel):
    symptom: str                     # 当前故障/症状
    modelType: Optional[str] = None


class TicketRequest(BaseModel):
    task: str                        # 操作任务（如"#1主变由运行转检修"）
    modelType: Optional[str] = None
