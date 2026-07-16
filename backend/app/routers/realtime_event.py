"""统一实时事件接入与主动运维闭环 API。"""
import re
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import ALERT_MANAGE, DOMAIN_USE, SYSTEM_CONFIG, has_perm
from app.core.response import BizError, success
from app.core.security import decode_token
from app.db.session import get_db
from app.dependencies import require_perm
from app.models.user import User
from app.schemas.realtime_event import (
    DeviceMappingUpsertRequest,
    RealtimeEventIn,
    RunRetryRequest,
    RunReviewRequest,
)
from app.services import realtime_event_service as service
from app.services.auth_service import get_user_by_id


router = APIRouter(prefix="/realtime", tags=["实时事件与主动运维"])

_TENANT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _external_identity(tenant_id: str) -> service.IngressIdentity:
    tenant = (tenant_id or "default").strip()
    if not _TENANT_RE.fullmatch(tenant):
        raise BizError("X-Tenant-Id 格式无效", 400)
    return service.IngressIdentity(
        tenant_id=tenant,
        actor="realtime-connector",
        auth_mode="connector",
    )


async def authenticate_realtime_ingress(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(default=None),
    x_event_token: Optional[str] = Header(default=None, alias="X-Event-Token"),
    x_event_signature: Optional[str] = Header(default=None, alias="X-Event-Signature"),
    x_event_timestamp: Optional[str] = Header(default=None, alias="X-Event-Timestamp"),
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
) -> service.IngressIdentity:
    """连接器使用租户绑定 token/HMAC；平台用户使用 JWT 与自身租户权限。"""
    bearer = ""
    if authorization and authorization.startswith("Bearer "):
        bearer = authorization[len("Bearer "):].strip()

    if x_event_token:
        identity = _external_identity(x_tenant_id)
        if service.verify_ingress_token(
            x_event_token, tenant_id=identity.tenant_id,
        ):
            return identity

    if x_event_signature and x_event_timestamp:
        identity = _external_identity(x_tenant_id)
        raw_body = await request.body()
        if service.verify_ingress_signature(
            x_event_signature,
            x_event_timestamp,
            raw_body,
            tenant_id=identity.tenant_id,
        ):
            return identity

    if bearer:
        try:
            payload = decode_token(bearer)
            user = await get_user_by_id(db, payload.get("sub", ""))
        except Exception:
            user = None
        if user and (
            has_perm(user.role, DOMAIN_USE) or has_perm(user.role, ALERT_MANAGE)
        ):
            return service.IngressIdentity(
                tenant_id=user.tenant_id or "default",
                actor=user.username,
                auth_mode="jwt",
            )
        if user:
            raise BizError("无权限接入实时事件", 403)
        # 兼容旧连接器把显式配置的实时 token 放在 Authorization Bearer；
        # 即使走此兼容入口，凭据仍只授权其绑定租户。
        identity = _external_identity(x_tenant_id)
        if service.verify_ingress_token(bearer, tenant_id=identity.tenant_id):
            return identity
    raise BizError("实时事件接入认证失败", 401)


def _value_error(exc: ValueError, not_found: bool = False) -> BizError:
    text = str(exc)
    missing = not_found or "不存在" in text
    return BizError(text, 404 if missing else 400)


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
async def receive_event(
    body: RealtimeEventIn,
    identity: service.IngressIdentity = Depends(authenticate_realtime_ingress),
    db: AsyncSession = Depends(get_db),
):
    """SCADA/OMS/PMS/generic 统一事件入口；eventId 在租户+源内幂等。"""
    data = await service.ingest_event(
        db,
        body,
        tenant_id=identity.tenant_id,
        actor=identity.actor,
    )
    message = "重复事件已幂等接收" if data["duplicate"] else "事件已接收"
    return success(data, message, 202)


@router.get("/events")
async def event_list(
    source: str = Query(default="", max_length=16),
    processing_status: str = Query(default="", alias="status", max_length=24),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOMAIN_USE)),
):
    data = await service.list_events(
        db,
        tenant_id=user.tenant_id,
        source=source.lower(),
        status=processing_status,
        page=page,
        size=size,
    )
    return success(data, "查询成功")


@router.get("/runs")
async def run_list(
    run_status: str = Query(default="", alias="status", max_length=24),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOMAIN_USE)),
):
    data = await service.list_runs(
        db,
        tenant_id=user.tenant_id,
        status=run_status,
        page=page,
        size=size,
    )
    return success(data, "查询成功")


@router.get("/runs/{run_id}")
async def run_detail(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(DOMAIN_USE)),
):
    data = await service.get_run(db, run_id, tenant_id=user.tenant_id)
    if not data:
        raise BizError("主动运维运行记录不存在", 404)
    return success(data, "查询成功")


@router.post("/runs/{run_id}/confirm")
async def confirm(
    run_id: str,
    body: RunReviewRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(ALERT_MANAGE)),
):
    try:
        data = await service.confirm_run(
            db,
            run_id,
            tenant_id=user.tenant_id,
            reviewer=user.username,
            note=body.note,
        )
    except ValueError as exc:
        raise _value_error(exc)
    return success(data, "建议已人工确认；未执行任何设备控制")


@router.post("/runs/{run_id}/reject")
async def reject(
    run_id: str,
    body: RunReviewRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(ALERT_MANAGE)),
):
    try:
        data = await service.reject_run(
            db,
            run_id,
            tenant_id=user.tenant_id,
            reviewer=user.username,
            note=body.note,
        )
    except ValueError as exc:
        raise _value_error(exc)
    return success(data, "建议已驳回")


@router.post("/runs/{run_id}/to-ticket")
async def to_ticket(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(ALERT_MANAGE)),
):
    try:
        data = await service.run_to_ticket(
            db,
            run_id,
            tenant_id=user.tenant_id,
            creator=user.username,
        )
    except ValueError as exc:
        raise _value_error(exc)
    return success(data, "已创建两票草稿；仍需走正式审核、签发与执行流程")


@router.post("/runs/{run_id}/retry")
async def retry(
    run_id: str,
    body: RunRetryRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(ALERT_MANAGE)),
):
    try:
        data = await service.retry_run(
            db,
            run_id,
            tenant_id=user.tenant_id,
            model_type=body.modelType,
        )
    except ValueError as exc:
        raise _value_error(exc)
    return success(data, "重试任务已提交")


@router.put("/device-mappings")
async def mapping_upsert(
    body: DeviceMappingUpsertRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(SYSTEM_CONFIG)),
):
    data = await service.upsert_device_mapping(db, body, tenant_id=user.tenant_id)
    return success(data, "设备映射已保存")


@router.get("/device-mappings")
async def mapping_list(
    source: str = Query(default="", max_length=16),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(SYSTEM_CONFIG)),
):
    data = await service.list_device_mappings(
        db,
        tenant_id=user.tenant_id,
        source=source.lower(),
        page=page,
        size=size,
    )
    return success(data, "查询成功")
