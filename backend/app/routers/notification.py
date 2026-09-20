"""通知中心 API：站内通知列表/未读数/已读 + WS 在线推送。"""
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import success
from app.core.security import decode_token
from app.core import ws_manager
from app.db.session import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.services import notification_service as svc

router = APIRouter(prefix="/notifications", tags=["通知中心"])


@router.get("")
async def list_notifications_api(
    unread: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = await svc.list_notifications(db, user.username, user.tenant_id,
                                        unread_only=unread, limit=limit)
    return success(rows)


@router.get("/unread-count")
async def unread_count_api(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return success({"count": await svc.unread_count(db, user.username, user.tenant_id)})


@router.post("/{notification_id}/read")
async def mark_read_api(
    notification_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return success(await svc.mark_read(db, notification_id, user.username, user.tenant_id))


@router.post("/read-all")
async def mark_all_read_api(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return success(await svc.mark_all_read(db, user.username, user.tenant_id))


@router.websocket("/ws")
async def notifications_ws(ws: WebSocket):
    """通知在线推送。?token=JWT 鉴权后按用户名定向登记。"""
    token = ws.query_params.get("token", "")
    try:
        payload = decode_token(token)
        username = str(payload.get("sub") or "")
        if not username:
            raise ValueError("empty sub")
    except Exception:
        await ws.accept()
        await ws.send_json({"type": "error", "message": "token 无效"})
        await ws.close()
        return
    await ws_manager.connect_notify(ws, username)
    try:
        while True:  # 心跳/保活：客户端可周期发 ping，服务端不主动断
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect_notify(ws)
