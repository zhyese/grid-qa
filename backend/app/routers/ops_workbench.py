"""独立运维工作台 API；薄路由，默认关闭。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.permissions import (
    DOC_MANAGE,
    DOC_READ,
    OPS_READ,
    OPS_REVIEW,
    OPS_WRITE,
    SYSTEM_CONFIG,
    has_perm,
)
from app.core.response import BizError, success
from app.db.session import get_db
from app.dependencies import require_perm
from app.models.user import User
from app.schemas.ops_workbench import (
    HandoverRequest,
    ImpactRequest,
    ImportRequest,
    ReviewRequest,
    TableQuery,
    TelemetryQuery,
)
from app.services import device_dossier_service as devices
from app.services import ops_catalog_service as catalog
from app.services import ops_snapshot_service as snapshots
from app.services import regulation_impact_service as impacts
from app.services import shift_handover_service as shifts
from app.services import table_lookup_service as tables
from app.services import telemetry_history_service as telemetry


def enabled():
    if not settings.OPS_WORKBENCH_ENABLE:
        raise BizError("运维工作台未启用（OPS_WORKBENCH_ENABLE）", 403)


router = APIRouter(
    prefix="/ops-workbench", tags=["运维工作台"], dependencies=[Depends(enabled)]
)


@router.get("/documents")
async def document_catalog(
    keyword: str = Query("", max_length=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_READ)),
):
    return success(await catalog.documents(db, user, keyword))


@router.get("/devices")
async def device_catalog(
    keyword: str = Query("", max_length=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(OPS_READ)),
):
    return success(await catalog.devices(db, user, keyword))


@router.get("/tables/{doc_id}/schema")
async def table_schema(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_READ)),
):
    return success(await catalog.table_schema(db, user, doc_id))


@router.post("/impacts")
async def impact(
    body: ImpactRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_MANAGE)),
):
    return success(await impacts.analyze(db, user, body))


@router.post("/handovers")
async def handover(
    body: HandoverRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(OPS_WRITE)),
):
    return success(await shifts.generate(db, user, body))


@router.get("/snapshots")
async def snapshot_list(
    kind: str = Query(pattern="^(impact|handover|dossier)$"),
    page: int = Query(1, ge=1, le=10000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(OPS_READ)),
):
    return success(await snapshots.list_snapshots(db, user, kind, page))


@router.get("/snapshots/{snapshot_id}")
async def snapshot_get(
    snapshot_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(OPS_READ)),
):
    return success(
        snapshots.serialize(await snapshots.get_snapshot(db, user, snapshot_id))
    )


@router.post("/snapshots/{snapshot_id}/review")
async def snapshot_review(
    snapshot_id: str,
    body: ReviewRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(OPS_WRITE)),
):
    row = await snapshots.get_snapshot(db, user, snapshot_id)
    permission = (
        DOC_MANAGE
        if row.kind == "impact"
        else OPS_REVIEW
        if body.action == "review"
        else OPS_WRITE
    )
    if not has_perm(user.role, permission):
        raise BizError(f"无权限：需要 {permission}", 403)
    return success(await snapshots.review_snapshot(db, user, snapshot_id, body))


@router.get("/devices/{device_id}")
async def device_get(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(OPS_READ)),
):
    return success(await devices.dossier(db, user, device_id))


@router.post("/devices/{device_id}/snapshots")
async def device_snapshot(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(OPS_WRITE)),
):
    return success(await devices.snapshot(db, user, device_id))


@router.post("/telemetry/import")
async def telemetry_import(
    body: ImportRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(SYSTEM_CONFIG)),
):
    return success(await telemetry.import_points(db, user, body))


@router.post("/telemetry/query")
async def telemetry_query(
    body: TelemetryQuery,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(OPS_READ)),
):
    return success(await telemetry.query(db, user, body))


@router.post("/tables/query")
async def table_query(
    body: TableQuery,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOC_READ)),
):
    return success(await tables.lookup(db, user, body))
