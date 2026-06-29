"""系统接口：登录 / 注册 / 操作日志查询。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import success
from app.db.session import get_db
from app.dependencies import get_current_user, require_admin
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest
from app.services.auth_service import authenticate, register_user
from app.services.log_service import query_logs, write_log

router = APIRouter(prefix="/system", tags=["系统-用户/权限"])


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
    """仅管理员可新增用户。"""
    data = await register_user(db, body.username, body.password, body.role)
    await write_log(db, admin.username, "注册用户", f"新增用户 {body.username}（{body.role}）")
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
    # admin 可查所有用户日志；operator 仅查自身
    operate_user = None if user.role == "admin" else user.username
    data = await query_logs(db, page, size, operate_user=operate_user)
    return success(data, "查询成功")
