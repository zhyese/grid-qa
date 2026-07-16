"""持久化任务队列与事件中心 API schema。"""
from datetime import datetime

from pydantic import BaseModel, Field


class EnqueueTaskRequest(BaseModel):
    taskType: str = Field(min_length=1, max_length=128)
    payload: dict = Field(default_factory=dict)
    queue: str = Field(default="default", min_length=1, max_length=64)
    idempotencyKey: str = Field(default="", max_length=191)
    priority: int = Field(default=0, ge=-100, le=100)
    maxAttempts: int = Field(default=3, ge=1, le=100)
    runAfter: datetime | None = None
    correlationId: str = Field(default="", max_length=64)
    causationId: str = Field(default="", max_length=64)


class PublishEventRequest(BaseModel):
    eventType: str = Field(min_length=1, max_length=160)
    payload: dict = Field(default_factory=dict)
    source: str = Field(default="api", max_length=128)
    aggregateType: str = Field(default="", max_length=128)
    aggregateId: str = Field(default="", max_length=128)
    idempotencyKey: str = Field(default="", max_length=191)
    headers: dict = Field(default_factory=dict)
    correlationId: str = Field(default="", max_length=64)
    causationId: str = Field(default="", max_length=64)
    schemaVersion: int = Field(default=1, ge=1)
    maxAttempts: int = Field(default=5, ge=1, le=100)


class TerminateTaskRequest(BaseModel):
    reason: str = Field(default="人工终止", max_length=1000)

