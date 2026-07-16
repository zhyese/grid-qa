"""两票全生命周期管理模型：草稿→审核→签发→执行→归档。"""
import enum

from sqlalchemy import Column, DateTime, Enum, Integer, String, Text, UniqueConstraint, func

from app.db.base import Base


class TicketStatus(str, enum.Enum):
    DRAFT = "draft"           # 草稿
    PENDING_REVIEW = "pending_review"  # 待审核
    REVIEWED = "reviewed"     # 已审核（通过/驳回）
    ISSUED = "issued"         # 已签发
    IN_EXECUTION = "in_execution"  # 执行中
    COMPLETED = "completed"   # 已完成
    ARCHIVED = "archived"     # 已归档
    REJECTED = "rejected"     # 驳回/作废


class TicketType(str, enum.Enum):
    OPERATION = "操作票"      # 操作票
    WORK = "工作票"           # 工作票


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_ref", name="uq_tickets_tenant_source_ref"),
    )

    id = Column(String(32), primary_key=True)
    tenant_id = Column(String(64), default="default", index=True)
    # 外部业务来源幂等键；普通人工建票为 NULL，可重复。
    source_ref = Column(String(128), nullable=True)

    # 基本信息
    ticket_type = Column(Enum(TicketType), nullable=False, default=TicketType.OPERATION)
    status = Column(Enum(TicketStatus), nullable=False, default=TicketStatus.DRAFT)
    title = Column(String(200), default="")          # 任务标题
    task = Column(Text, default="")                  # 操作任务描述
    device = Column(String(200), default="")         # 涉及设备
    location = Column(String(200), default="")       # 作业地点

    # 内容（结构化 JSON）
    steps = Column(Text, default="")                 # 操作步骤 JSON 数组
    safety_measures = Column(Text, default="")       # 安全措施 JSON 数组
    risks = Column(Text, default="")                 # 风险点 JSON 数组
    notes = Column(Text, default="")                 # 备注

    # 参与人
    creator = Column(String(64), default="")         # 创建人
    reviewer = Column(String(64), default="")        # 审核人
    issuer = Column(String(64), default="")          # 签发人
    executor = Column(String(64), default="")        # 执行人
    supervisor = Column(String(64), default="")      # 监护人

    # 时间线
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    reviewed_at = Column(DateTime, nullable=True)
    issued_at = Column(DateTime, nullable=True)
    executed_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    archived_at = Column(DateTime, nullable=True)

    # 审核信息
    review_score = Column(Integer, default=0)        # 审核得分（0-100）
    review_comment = Column(Text, default="")        # 审核意见
    audit_report = Column(Text, default="")          # 审核报告 JSON 字符串

    # 执行记录
    execution_log = Column(Text, default="")         # 执行日志 JSON 数组
    deviation = Column(Text, default="")             # 偏差记录

    # 统计
    version = Column(Integer, default=1)             # 版本号
    is_deleted = Column(Integer, default=0)          # 软删
