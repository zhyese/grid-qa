"""WebSocket 连接管理（告警实时推送用）。

维护一个告警订阅客户端集合，alerts_webhook 收到告警后 broadcast，
前端 Admin 告警 Tab 订阅 /ws/alerts 即可实时收推送，不再轮询。
"""
from fastapi import WebSocket

_clients: set[WebSocket] = set()


async def connect(ws: WebSocket) -> None:
    await ws.accept()
    _clients.add(ws)


def disconnect(ws: WebSocket) -> None:
    _clients.discard(ws)


async def broadcast(message: dict) -> None:
    """向所有订阅客户端推送（断连的剔除）。"""
    dead = []
    for ws in list(_clients):
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


def client_count() -> int:
    return len(_clients)
