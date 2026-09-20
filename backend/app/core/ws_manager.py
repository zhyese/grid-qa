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


# ===== N3 数字孪生 WebSocket 通道（多站点切片：订阅按站点隔离） =====

_twin_clients: dict[WebSocket, str] = {}  # ws → 订阅的 station_id（空=全站）


async def connect_twin(ws: WebSocket, station_id: str = "") -> None:
    """接受数字孪生 WebSocket 连接，按站点加入 twin 订阅集合。"""
    await ws.accept()
    _twin_clients[ws] = station_id or ""


def disconnect_twin(ws: WebSocket) -> None:
    """断开孪生订阅（幂等）。"""
    _twin_clients.pop(ws, None)


async def broadcast_twin(message: dict) -> None:
    """向孪生订阅客户端推送告警定位/状态变更（断连的剔除）。

    站点隔离：message 带 stationId 时只推给订阅该站的客户端；
    不带（如 layout-refresh 全局通知）推给全部。
    """
    target_station = str(message.get("stationId") or "")
    dead = []
    for ws, subscribed in list(_twin_clients.items()):
        if target_station and subscribed and subscribed != target_station:
            continue
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _twin_clients.pop(ws, None)


def twin_client_count() -> int:
    return len(_twin_clients)


# ===== 定向通知通道（通知中心：待签/报告完成/演练复盘，按 username 点对点） =====

_notify_clients: dict[WebSocket, str] = {}  # ws → 鉴权用户名


async def connect_notify(ws: WebSocket, username: str) -> None:
    """接受通知订阅连接，按用户名登记（同一用户多端在线各自收到）。"""
    await ws.accept()
    _notify_clients[ws] = username


def disconnect_notify(ws: WebSocket) -> None:
    _notify_clients.pop(ws, None)


async def send_to_user(username: str, message: dict) -> int:
    """定向推送给某用户的全部在线连接（断连剔除），返回送达数。"""
    delivered = 0
    dead = []
    for ws, name in list(_notify_clients.items()):
        if name != username:
            continue
        try:
            await ws.send_json(message)
            delivered += 1
        except Exception:
            dead.append(ws)
    for ws in dead:
        _notify_clients.pop(ws, None)
    return delivered
