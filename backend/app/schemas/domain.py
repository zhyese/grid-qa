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


class TicketAuditRequest(BaseModel):
    ticketText: str                       # 已填票据全文（粘贴）
    ticketType: str = "操作票"             # 操作票 / 工作票
    modelType: Optional[str] = None


class DiagnoseAgentRequest(BaseModel):
    symptom: str                        # 故障症状描述
    modelType: Optional[str] = None


class DiagnoseDebateRequest(BaseModel):
    symptom: str                        # 故障症状描述
    modelType: Optional[str] = None


# ===== 两票全生命周期 =====


class TicketCreateRequest(BaseModel):
    ticketType: str = "操作票"
    task: str = ""
    device: str = ""
    location: str = ""
    steps: list[str] = []
    safety: list[str] = []
    risks: list[str] = []
    notes: str = ""


class TicketListRequest(BaseModel):
    status: str = ""
    ticketType: str = ""
    creator: str = ""
    page: int = 1
    size: int = 20


class TicketReviewRequest(BaseModel):
    approved: bool
    comment: str = ""


class TicketExecuteRequest(BaseModel):
    executor: str = ""
    supervisor: str = ""
    log: str = ""
    deviation: str = ""
