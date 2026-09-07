"""工作流编排 API 入参 schema。"""
from pydantic import BaseModel, Field


class WorkflowSave(BaseModel):
    name: str = Field(..., max_length=64)
    description: str = Field("", max_length=255)
    graph: dict = Field(..., description="{nodes:[{id,type,key,label,params,x,y}], edges:[{id,source,target,branch?}]}")


class WorkflowUpdate(BaseModel):
    name: str | None = Field(None, max_length=64)
    description: str | None = Field(None, max_length=255)
    graph: dict | None = None
    enabled: bool | None = None


class WorkflowRunCreate(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    vars: dict[str, str] = Field(default_factory=dict, description="input 节点附加变量")
