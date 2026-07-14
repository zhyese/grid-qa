"""N3 数字孪生 API（场景布局/设备详情/总览/告警订阅 WebSocket）。"""
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi import Request

from app.core.response import BizError, success
from app.core.ws_manager import connect_twin, disconnect_twin
from app.services import twin_service

router = APIRouter(prefix="/twin", tags=["数字孪生"])


@router.get("/station/layout")
async def station_layout(
    stationId: str = Query("110kV-demo", description="站点 ID"),
):
    """获取站点布局模板（areas + devices 坐标）。"""
    data = await twin_service.get_station_layout(stationId)
    return success(data=data)


@router.get("/station/overview")
async def station_overview(
    stationId: str = Query("110kV-demo", description="站点 ID"),
):
    """获取站点总览：设备列表 + 各设备状态（riskScore/颜色/告警/闪烁）。"""
    data = await twin_service.get_station_overview(stationId)
    return success(data=data)


@router.get("/device/{device_id}/detail")
async def device_detail(device_id: str):
    """获取设备详情：风险/知识图谱上下文/故障传播链/告警。"""
    data = await twin_service.get_device_detail(device_id)
    if data.get("error"):
        raise BizError(data["error"], 404)
    return success(data=data)


@router.get("/device/{device_id}/fault-chain")
async def device_fault_chain(
    device_id: str,
    depth: int = Query(3, ge=1, le=5, description="传播链深度"),
):
    """获取设备故障传播链（多跳路径）。"""
    data = await twin_service.get_fault_chain(device_id, depth)
    return success(data=data)


@router.post("/alert/push")
async def push_alert(request: Request):
    """告警定位推送（内部调用：alert_disposal_service → twin_service → WebSocket 广播）。

    Body: {severity, title, device/deviceId, summary}
    """
    body = await request.json()
    data = await twin_service.push_alert_location(body)
    return success(data=data)


@router.websocket("/ws/twin")
async def twin_ws(ws: WebSocket):
    """数字孪生 WebSocket 通道：订阅告警定位推送。"""
    await connect_twin(ws)
    try:
        while True:
            await ws.receive_text()  # 保持连接（忽略客户端消息）
    except WebSocketDisconnect:
        pass
    finally:
        disconnect_twin(ws)
