"""N7 运维报告智能生成 API（/api/ops-reports，OPS_REPORT_ENABLE 开时注册）。"""
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import REPORT_MANAGE, REPORT_READ
from app.core.response import BizError, success
from app.db.session import get_db
from app.dependencies import require_perm
from app.models.user import User
from app.schemas.ops_report import ReportCreate
from app.services import ops_report_service as svc

router = APIRouter(prefix="/ops-reports", tags=["运维报告"])


@router.get("/meta")
async def report_meta_api(user: User = Depends(require_perm(REPORT_READ))):
    """报告类型元数据（下拉 + 默认窗口）。"""
    return success(svc.type_meta())


@router.get("")
async def list_reports_api(
    report_type: str = Query("", alias="reportType"),
    status: str = Query(""),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(REPORT_READ)),
):
    total, rows = await svc.list_reports(db, user.tenant_id, report_type=report_type,
                                         status=status, page=page, size=size)
    return success({"total": total, "items": rows, "page": page, "size": size})


@router.post("")
async def create_report_api(
    body: ReportCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(REPORT_MANAGE)),
):
    row = await svc.create_report(
        db, tenant_id=user.tenant_id, username=user.username,
        report_type=body.reportType, days=body.days, device_id=body.deviceId,
        model_type=body.modelType, title=body.title)
    return success(row)


@router.get("/{report_id}")
async def get_report_api(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(REPORT_READ)),
):
    return success(await svc.get_report_detail(db, report_id, user.tenant_id))


@router.post("/{report_id}/regenerate")
async def regenerate_report_api(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(REPORT_MANAGE)),
):
    return success(await svc.regenerate(db, report_id, user.tenant_id))


@router.delete("/{report_id}")
async def delete_report_api(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(REPORT_MANAGE)),
):
    return success(await svc.delete_report(db, report_id, user.tenant_id))


@router.get("/{report_id}/export/docx")
async def export_report_api(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(REPORT_READ)),
):
    detail = await svc.get_report_detail(db, report_id, user.tenant_id)
    if detail.get("status") != "done":
        raise BizError("报告尚未生成完成，无法导出", 400)
    content = svc.build_report_docx(detail)
    filename = f"{detail['title']}.docx"
    from urllib.parse import quote

    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )
