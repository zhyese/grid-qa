"""ORM 模型导出。

启动建表仍由 ``app.db.init_db`` 显式导入；这里提供跨模块使用时的稳定入口。
"""
from app.models.domain_event import DomainEvent, EventDelivery
from app.models.knowledge_governance import (
    KnowledgeDocumentMetadata,
    KnowledgeGovernanceIssue,
    KnowledgeGovernanceReview,
)
from app.models.persistent_task import PersistentTask
from app.models.realtime_event import ProactiveOpsRun, RealtimeDeviceMapping, RealtimeEvent

__all__ = [
    "DomainEvent", "EventDelivery", "PersistentTask",
    "KnowledgeDocumentMetadata", "KnowledgeGovernanceIssue", "KnowledgeGovernanceReview",
    "RealtimeEvent", "RealtimeDeviceMapping", "ProactiveOpsRun",
]
