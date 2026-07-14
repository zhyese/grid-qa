"""WebSocket 连接管理（告警实时推送 + 数字孪生告警定位推送）。

维护多组订阅客户端集合：
- _clients: 告警订阅（Admin 告警 Tab 实时推送）
- _twin_clients: 数字孪生订阅（DigitalTwin.vue 告警定位+闪烁推送）
broadcast / broadcast_twin 分别推送，断连的自动剔除。
"""
from fastapi import WebSocket

_clients: set[WebSocket] = set()
_twin_clients: set[WebSocket] = set()


async def connect(ws: WebSocket) -> None:
    await ws.accept()
    _clients.add(ws)


def disconnect(ws: WebSocket) -> None:
    _clients.discard(ws)


async def broadcast(message: dict) -> None:
    """向所有告警订阅客户端推送（断连的剔除）。"""
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


# ===== N3 数字孪生 WebSocket 通道 =====

async def connect_twin(ws: WebSocket) -> None:
    """接受数字孪生 WebSocket 连接，加入 twin 订阅集合。"""
    await ws.accept()
    _twin_clients.add(ws)


def disconnect_twin(ws: WebSocket) -> None:
    _twin_clients.discard(ws)


async def broadcast_twin(message: dict) -> None:
    """向所有数字孪生订阅客户端推送告警定位/状态变更（断连的剔除）。"""
    dead = []
    for ws in list(_twin_clients):
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _twin_clients.discard(ws)


def twin_client_count() -> int:
    return len(_twin_clients)
