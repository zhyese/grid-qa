"""可视化工作流编排服务：CRUD + DAG 校验 + 运行编排（引擎在 workflow_engine）。"""
import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.obs import degraded
from app.core.response import BizError
from app.models.workflow import Workflow, WorkflowRun
from app.services.workflow_engine import execute_run, utcnow, validate_graph


def _row(w: Workflow) -> dict:
    return {"id": w.id, "name": w.name, "description": w.description,
            "graph": w.graph, "enabled": bool(w.enabled), "version": w.version,
            "createdBy": w.created_by, "createdAt": w.created_at.isoformat() if w.created_at else None,
            "updatedAt": w.updated_at.isoformat() if w.updated_at else None}


def _run_row(r: WorkflowRun, with_states: bool = True) -> dict:
    d = {"id": r.id, "workflowId": r.workflow_id, "workflowName": r.workflow_name,
         "workflowVersion": r.workflow_version, "status": r.status,
         "input": r.input, "output": r.output, "error": r.error,
         "createdBy": r.created_by,
         "startedAt": r.started_at.isoformat() if r.started_at else None,
         "finishedAt": r.finished_at.isoformat() if r.finished_at else None,
         "durationMs": r.duration_ms}
    if with_states:
        d["nodeStates"] = r.node_states or []
    return d


async def list_workflows(db: AsyncSession, tenant_id: str, keyword: str = "",
                         page: int = 1, size: int = 20) -> tuple[int, list[dict]]:
    conds = [Workflow.tenant == tenant_id]
    if keyword:
        conds.append(Workflow.name.contains(keyword))
    total = len((await db.execute(select(Workflow.id).where(*conds))).all())
    rows = (await db.execute(
        select(Workflow).where(*conds)
        .order_by(Workflow.updated_at.desc()).offset((page - 1) * size).limit(size))
    ).scalars().all()
    return total, [_row(w) for w in rows]


async def get_workflow(db: AsyncSession, wf_id: str, tenant_id: str) -> Workflow:
    wf = (await db.execute(
        select(Workflow).where(Workflow.id == wf_id, Workflow.tenant == tenant_id))).scalar_one_or_none()
    if wf is None:
        raise BizError("工作流不存在", 404)
    return wf


async def create_workflow(db: AsyncSession, name: str, description: str,
                          graph: dict, tenant_id: str, username: str) -> dict:
    if not name or not name.strip():
        raise BizError("名称不能为空", 400)
    if (await db.execute(select(Workflow.id).where(
            Workflow.name == name.strip(), Workflow.tenant == tenant_id))).first():
        raise BizError("同名工作流已存在", 400)
    errs = validate_graph(graph)
    if errs:
        raise BizError("图校验失败：" + "；".join(errs), 400)
    wf = Workflow(id=uuid.uuid4().hex, name=name.strip(), description=description or "",
                  graph=graph, tenant=tenant_id, created_by=username)
    db.add(wf)
    await db.commit()
    return _row(wf)


async def update_workflow(db: AsyncSession, wf_id: str, tenant_id: str,
                          name: str | None = None, description: str | None = None,
                          graph: dict | None = None, enabled: bool | None = None) -> dict:
    wf = await get_workflow(db, wf_id, tenant_id)
    if graph is not None:
        errs = validate_graph(graph)
        if errs:
            raise BizError("图校验失败：" + "；".join(errs), 400)
        wf.graph = graph
        wf.version += 1
    if name is not None and name.strip():
        wf.name = name.strip()
    if description is not None:
        wf.description = description
    if enabled is not None:
        wf.enabled = 1 if enabled else 0
    await db.commit()
    await db.refresh(wf)  # onupdate(func.now()) 后属性过期，async session 禁属性级懒 IO
    return _row(wf)


async def delete_workflow(db: AsyncSession, wf_id: str, tenant_id: str) -> dict:
    wf = await get_workflow(db, wf_id, tenant_id)
    await db.delete(wf)
    await db.commit()
    return {"id": wf_id}


async def start_run(db: AsyncSession, wf: Workflow, query: str,
                    extra_vars: dict | None, tenant_id: str, username: str,
                    user_role: str) -> dict:
    """落 run 行（running）→ 丢后台任务执行 → 前端轮询 GET /workflows/runs/{id}。"""
    if not (query or "").strip():
        raise BizError("query 不能为空", 400)
    run = WorkflowRun(id=uuid.uuid4().hex, workflow_id=wf.id, workflow_name=wf.name,
                      workflow_version=wf.version, status="running",
                      input={"query": query, "vars": extra_vars or {}},
                      tenant=tenant_id, created_by=username, started_at=utcnow())
    db.add(run)
    await db.commit()
    try:
        asyncio.create_task(execute_run(run.id))
    except Exception as e:  # create_task 拒绝（事件循环已关等）→ 立刻标失败
        degraded("workflow_schedule_run", e)
        run.status = "failed"
        run.error = f"调度执行失败：{e}"
        await db.commit()
    return _run_row(run)


async def list_runs(db: AsyncSession, wf_id: str, tenant_id: str,
                    page: int = 1, size: int = 20) -> tuple[int, list[dict]]:
    base = select(WorkflowRun).where(WorkflowRun.workflow_id == wf_id,
                                     WorkflowRun.tenant == tenant_id)
    total = len((await db.execute(
        select(WorkflowRun.id).where(WorkflowRun.workflow_id == wf_id,
                                     WorkflowRun.tenant == tenant_id))).all())
    rows = (await db.execute(
        base.order_by(WorkflowRun.started_at.desc()).offset((page - 1) * size).limit(size))
    ).scalars().all()
    return total, [_run_row(r, with_states=False) for r in rows]


async def get_run(db: AsyncSession, run_id: str, tenant_id: str) -> dict:
    r = (await db.execute(
        select(WorkflowRun).where(WorkflowRun.id == run_id,
                                  WorkflowRun.tenant == tenant_id))).scalar_one_or_none()
    if r is None:
        raise BizError("运行记录不存在", 404)
    return _run_row(r)


async def list_personas() -> list[str]:
    """Agent 节点下拉：code personas + DB 覆盖名，去重。"""
    from app.services.agent_personas import (ALERT_PERSONA, DIAGNOSE_PERSONA,
                                             EVIDENCE_PERSONA, OPS_PLANNER_PERSONA,
                                             PROACTIVE_DIAGNOSIS_PERSONA, QA_PERSONA)
    names = [p.name for p in (QA_PERSONA, DIAGNOSE_PERSONA, ALERT_PERSONA,
                              EVIDENCE_PERSONA, OPS_PLANNER_PERSONA,
                              PROACTIVE_DIAGNOSIS_PERSONA)]
    try:
        from app.services.persona_store import list_configs
        names += [k for k in await list_configs() if k not in names]
    except Exception as e:  # noqa: BLE001 — 下拉数据源降级不影响主功能
        degraded("workflow_list_personas", e)
    return names
