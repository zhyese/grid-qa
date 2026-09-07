"""可视化工作流编排 API（/api/workflows，WORKFLOW_ENABLE 开时注册）。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import WORKFLOW_MANAGE, WORKFLOW_READ
from app.core.response import BizError, success
from app.db.session import get_db
from app.dependencies import require_perm
from app.models.user import User
from app.schemas.workflow import WorkflowRunCreate, WorkflowSave, WorkflowUpdate
from app.services import workflow_service

router = APIRouter(prefix="/workflows", tags=["工作流编排"])


@router.get("")
async def list_workflows_api(
    keyword: str = Query("", max_length=64),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(WORKFLOW_READ)),
):
    total, rows = await workflow_service.list_workflows(
        db, tenant_id=user.tenant_id, keyword=keyword.strip(), page=page, size=size)
    return success({"total": total, "items": rows, "page": page, "size": size})


@router.get("/meta")
async def workflow_meta_api(user: User = Depends(require_perm(WORKFLOW_READ))):
    """编辑器元数据：节点类型说明 + persona 下拉。"""
    return success({
        "nodeTypes": [
            {"type": "input", "label": "输入", "desc": "工作流入参（query），整图唯一起点"},
            {"type": "retrieval", "label": "知识检索", "desc": "混合检索规程/手册，输出 context"},
            {"type": "kg", "label": "图谱因果链", "desc": "查设备-故障-处置因果链"},
            {"type": "llm", "label": "LLM 生成", "desc": "按 prompt 模板生成文本"},
            {"type": "agent", "label": "Agent", "desc": "ReAct 智能体（persona+工具）"},
            {"type": "condition", "label": "条件分支", "desc": "contains/not_contains/is_empty"},
            {"type": "output", "label": "输出", "desc": "终点，产出最终答案"},
        ],
        "personas": await workflow_service.list_personas(),
    })


@router.post("")
async def create_workflow_api(
    body: WorkflowSave,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(WORKFLOW_MANAGE)),
):
    row = await workflow_service.create_workflow(
        db, name=body.name, description=body.description, graph=body.graph,
        tenant_id=user.tenant_id, username=user.username)
    return success(row)


@router.get("/{wf_id}")
async def get_workflow_api(
    wf_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(WORKFLOW_READ)),
):
    wf = await workflow_service.get_workflow(db, wf_id, user.tenant_id)
    return success(workflow_service._row(wf))


@router.put("/{wf_id}")
async def update_workflow_api(
    wf_id: str,
    body: WorkflowUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(WORKFLOW_MANAGE)),
):
    row = await workflow_service.update_workflow(
        db, wf_id, user.tenant_id, name=body.name, description=body.description,
        graph=body.graph, enabled=body.enabled)
    return success(row)


@router.delete("/{wf_id}")
async def delete_workflow_api(
    wf_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(WORKFLOW_MANAGE)),
):
    return success(await workflow_service.delete_workflow(db, wf_id, user.tenant_id))


@router.post("/{wf_id}/run")
async def run_workflow_api(
    wf_id: str,
    body: WorkflowRunCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(WORKFLOW_MANAGE)),
):
    wf = await workflow_service.get_workflow(db, wf_id, user.tenant_id)
    if not wf.enabled:
        raise BizError("工作流已停用，请先启用再运行", 400)
    row = await workflow_service.start_run(
        db, wf, query=body.query, extra_vars=body.vars,
        tenant_id=user.tenant_id, username=user.username, user_role=user.role)
    return success(row)


@router.get("/{wf_id}/runs")
async def list_workflow_runs_api(
    wf_id: str,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(WORKFLOW_READ)),
):
    await workflow_service.get_workflow(db, wf_id, user.tenant_id)  # 租户校验
    total, rows = await workflow_service.list_runs(db, wf_id, user.tenant_id,
                                                  page=page, size=size)
    return success({"total": total, "items": rows, "page": page, "size": size})


@router.get("/runs/{run_id}")
async def get_workflow_run_api(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(WORKFLOW_READ)),
):
    return success(await workflow_service.get_run(db, run_id, user.tenant_id))
