"""知识时效与冲突治理 API schema。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


class _CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class KnowledgeMetadataUpsert(_CamelModel):
    """治理元数据支持分步补录；缺项由治理扫描持续提示。"""

    owner: str | None = Field(default=None, max_length=64)
    applicable_region: str | None = Field(default=None, max_length=256)
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    is_permanent: bool | None = None
    review_interval_days: int | None = Field(default=None, ge=1, le=3650)
    next_review_at: datetime | None = None
    version_label: str | None = Field(default=None, max_length=64)
    version_status: Literal["draft", "active", "superseded", "withdrawn"] | None = None

    @model_validator(mode="after")
    def validate_dates(self):
        if self.is_permanent is True and self.expires_at is not None:
            raise ValueError("永久有效文档不能同时设置失效时间")
        if self.effective_at and self.expires_at and self.effective_at > self.expires_at:
            raise ValueError("生效时间不能晚于失效时间")
        return self


class GovernanceScanRequest(_CamelModel):
    expiry_warning_days: int = Field(default=30, ge=1, le=365)
    include_conflicts: bool = True
    max_documents: int = Field(default=100, ge=1, le=500)
    max_chunks_per_document: int = Field(default=80, ge=1, le=500)
    document_ids: list[str] = Field(default_factory=list, max_length=500)


class GovernanceIssueReviewRequest(_CamelModel):
    status: Literal["open", "confirmed", "resolved", "ignored"]
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def require_resolution_note(self):
        if self.status in {"resolved", "ignored"} and not self.note:
            raise ValueError("解决或忽略问题时必须填写审核说明")
        return self
