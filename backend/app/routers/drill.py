"""N5 故障仿真演练沙箱 API（/api/drills，DRILL_SANDBOX_ENABLE 开时注册）。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import DRILL_MANAGE, DRILL_READ
from app.core.response import success
from app.db.session import get_db
from app.dependencies import require_perm
from app.models.user import User
from app.schemas.drill import (
    FaultChainGen,
    RunAction,
    RunStart,
    ScenarioCreate,
    ScenarioUpdate,
)
from app.services import drill_service as svc

router = APIRouter(prefix="/drills", tags=["故障演练沙箱"])


# ---------- 剧本 ----------

@router.get("/scenarios")
async def list_scenarios_api(
    keyword: str = Query("", max_length=64),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DRILL_READ)),
):
    total, rows = await svc.list_scenarios(db, user.tenant_id, keyword=keyword.strip(),
                                           page=page, size=size)
    return success({"total": total, "items": rows, "page": page, "size": size})


@router.post("/scenarios")
async def create_scenario_api(
    body: ScenarioCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DRILL_MANAGE)),
):
    return success(await svc.create_scenario(
        db, user.tenant_id, user.username, body.name, body.description,
        body.stationId, body.faultDevice, body.faultDesc,
        [e.model_dump() for e in body.propagation],
        [c.model_dump() for c in body.checklist], body.difficulty))


@router.post("/scenarios/from-fault-chain")
async def scenario_from_fault_chain_api(
    body: FaultChainGen,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DRILL_MANAGE)),
):
    """孪生故障链一键生成剧本（生成后可编辑）。"""
    return success(await svc.scenario_from_fault_chain(
        db, user.tenant_id, user.username, body.stationId, body.deviceId, body.name))


@router.get("/scenarios/{scenario_id}")
async def get_scenario_api(
    scenario_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DRILL_READ)),
):
    return success(svc._scenario_row(await svc.get_scenario(db, scenario_id, user.tenant_id)))


@router.put("/scenarios/{scenario_id}")
async def update_scenario_api(
    scenario_id: str,
    body: ScenarioUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DRILL_MANAGE)),
):
    kwargs = {}
    if body.propagation is not None:
        kwargs["propagation"] = [e.model_dump() for e in body.propagation]
    if body.checklist is not None:
        kwargs["checklist"] = [c.model_dump() for c in body.checklist]
    for k, v in (("name", body.name), ("description", body.description),
                 ("station_id", body.stationId), ("fault_device", body.faultDevice),
                 ("fault_desc", body.faultDesc), ("difficulty", body.difficulty),
                 ("enabled", body.enabled)):
        if v is not None:
            kwargs[k] = v
    return success(await svc.update_scenario(db, scenario_id, user.tenant_id, **kwargs))


@router.delete("/scenarios/{scenario_id}")
async def delete_scenario_api(
    scenario_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DRILL_MANAGE)),
):
    return success(await svc.delete_scenario(db, scenario_id, user.tenant_id))


# ---------- 演练 ----------

@router.post("/runs")
async def start_run_api(
    body: RunStart,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DRILL_MANAGE)),
):
    return success(await svc.start_run(db, user.tenant_id, user.username, body.scenarioId))


@router.get("/runs")
async def list_runs_api(
    scenarioId: str = Query(""),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DRILL_READ)),
):
    total, rows = await svc.list_runs(db, user.tenant_id, scenario_id=scenarioId,
                                      page=page, size=size)
    return success({"total": total, "items": rows, "page": page, "size": size})


@router.get("/stats")
async def drill_stats_api(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DRILL_READ)),
):
    return success(await svc.drill_stats(db, user.tenant_id))


@router.get("/runs/{run_id}")
async def run_state_api(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DRILL_READ)),
):
    return success(await svc.run_state(db, run_id, user.tenant_id))


@router.post("/runs/{run_id}/actions")
async def record_action_api(
    run_id: str,
    body: RunAction,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DRILL_MANAGE)),
):
    return success(await svc.record_action(db, run_id, user.tenant_id,
                                           user.username, body.action))


@router.post("/runs/{run_id}/finish")
async def finish_run_api(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DRILL_MANAGE)),
):
    return success(await svc.finish_run(db, run_id, user.tenant_id, user.username))


@router.post("/runs/{run_id}/abort")
async def abort_run_api(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DRILL_MANAGE)),
):
    return success(await svc.abort_run(db, run_id, user.tenant_id, user.username))
