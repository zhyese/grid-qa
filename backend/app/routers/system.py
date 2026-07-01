"""系统接口：登录 / 注册 / 操作日志（角色+时间过滤） / 配置（管理员，Redis 持久化）。"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import BizError, success
from app.db.session import get_db
from app.dependencies import get_current_user, require_admin
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest
from app.schemas.system import MilvusConfigRequest, ModelConfigRequest
from app.services import config_service, log_service
from app.services.auth_service import authenticate, register_user
from app.services.log_service import query_logs, write_log

router = APIRouter(prefix="/system", tags=["系统-用户/权限/配置"])


@router.post("/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    data = await authenticate(db, body.username, body.password)
    await write_log(db, body.username, "登录", f"用户 {body.username} 登录系统")
    return success(data, "登录成功")


@router.post("/register")
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    data = await register_user(db, body.username, body.password, body.role, body.tenantId)
    await write_log(db, admin.username, "注册用户", f"新增用户 {body.username}（{body.role}/{body.tenantId}）")
    return success(data, "注册成功")


@router.get("/logs")
async def logs(
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    startTime: str = Query(None),
    endTime: str = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    operate_user = None if user.role == "admin" else user.username
    data = await query_logs(
        db, page, size, operate_user=operate_user, start_time=startTime, end_time=endTime
    )
    return success(data, "查询成功")


@router.post("/config/milvus")
async def config_milvus(body: MilvusConfigRequest, admin: User = Depends(require_admin)):
    data = await config_service.update_milvus_config(body.indexType, body.param)
    return success(data, "配置成功")


@router.post("/config/model")
async def config_model(body: ModelConfigRequest, admin: User = Depends(require_admin)):
    data = await config_service.update_model_config(body.modelType, body.param)
    return success(data, "配置成功")


@router.get("/config/milvus")
async def get_milvus_config_route(admin: User = Depends(require_admin)):
    return success(await config_service.get_milvus_config(), "查询成功")


@router.get("/config/model")
async def get_model_config_route(admin: User = Depends(require_admin)):
    return success(await config_service.get_model_config(), "查询成功")


@router.get("/health/providers")
async def health_providers(admin: User = Depends(require_admin)):
    """主动探测当前 LLM/Embedding provider 是否真实可用（抓欠费/配额/key失效/网络问题）。

    会消耗少量 token（LLM ping + embed 一条短文本），按需调用；常规探活用 GET /health（只看配置态）。
    """
    from app.providers.factory import check_embedding_health, check_llm_health

    llm = await check_llm_health()
    emb = await check_embedding_health()
    all_ok = llm["status"] == "ok" and emb["status"] == "ok"
    return success(
        {
            "status": "healthy" if all_ok else "degraded",
            "llm": llm,
            "embedding": emb,
        },
        "provider 探测完成",
    )


@router.get("/config/nacos")
async def nacos_config_route(admin: User = Depends(require_admin)):
    """拉取 Nacos 配置中心配置（测试连通 + 查看覆盖项）。"""
    from app.clients.nacos_client import fetch_config
    from app.config import settings

    try:
        cfg = await fetch_config()
        return success(
            {"server": settings.NACOS_SERVER, "dataId": settings.NACOS_DATA_ID,
             "group": settings.NACOS_GROUP, "items": len(cfg), "config": cfg},
            "拉取成功",
        )
    except Exception as e:
        return success(
            {"server": settings.NACOS_SERVER, "error": f"{type(e).__name__}: {e}"[:200]},
            "拉取失败（确认 nacos 已启动：docker compose up -d nacos）",
        )


@router.post("/alerts/webhook")
async def alerts_webhook(
    request: Request,
    token: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Grafana alerting contact point 回调：接收告警 → 落操作日志(operate_type=告警) + 计指标。

    免 JWT 鉴权（Grafana webhook 不带我们的 token），改用共享密钥 query 校验防滥发。
    落库后管理员在「系统管理 → 操作日志/告警」直接看到，形成"指标→告警→可见"闭环，
    不依赖外部钉钉/企微凭据。payload 取 Grafana 标准 alertmanager 风格 {alerts:[...]}。
    """
    from app.config import settings

    if token != settings.ALERT_WEBHOOK_TOKEN:
        raise BizError("告警 webhook token 无效", 403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    alerts = body.get("alerts") or []
    from app.core import metrics

    for a in alerts:
        labels = a.get("labels") or {}
        ann = a.get("annotations") or {}
        sev = labels.get("severity", "warning")
        title = labels.get("alertname", "未知告警")
        summary = ann.get("summary", "")
        state = a.get("status", "firing")
        content = f"[{sev}] {title}" + (f"：{summary}" if summary else "") + f"（{state}）"
        await write_log(db, "Grafana", "告警", content[:500])
        try:
            metrics.ALERT_RECEIVED.labels(sev).inc()
        except Exception:
            pass
    return success({"received": len(alerts)}, "告警已接收")


@router.get("/alerts")
async def alerts(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """告警列表（操作日志中 operate_type=告警），管理员。"""
    data = await query_logs(db, page, size, operate_type="告警")
    return success(data, "查询成功")
