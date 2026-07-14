"""N3 数字孪生变电站服务：设备-空间映射 + 状态聚合 + 告警推送 + 故障链。

复用现有服务：
- fault_prediction_service.predict()：riskScore → 设备着色
- kg_service.graph_context() / get_paths()：故障传播链
- alert_disposal_service：告警状态
- ws_manager.broadcast_twin()：告警定位 WebSocket 推送
"""
import datetime
import json
import os
from typing import Any

from app.config import settings
from app.core.obs import degraded
from app.core.otel_genai import get_trace_id, trace_span

# 布局缓存（启动时加载一次）
_layout_cache: dict[str, dict] = {}

# 设备类型枚举（决定 Three.js 几何体+图标+默认颜色+模型描述符）
# model 字段：前端按 model 选择专属几何体工厂函数（不再是统一 BoxGeometry）
DEVICE_TYPES: dict[str, dict] = {
    # 主变压器：大型油浸式变压器 + 油枕 + 高压套管 + 散热片
    "main_transformer": {
        "icon": "🔀",
        "color": 0x5D6D7E,
        "defaultSize": [2.5, 3, 2.5],
        "model": "transformer",
        "label": "主变压器",
    },
    # 断路器：SF6 罐式 + 支柱绝缘子 + 操作机构
    "circuit_breaker": {
        "icon": "⚡",
        "color": 0xC0392B,
        "defaultSize": [1, 2, 1],
        "model": "breaker",
        "label": "断路器",
    },
    # 隔离开关：双柱 + 水平刀闸
    "disconnector": {
        "icon": "🔌",
        "color": 0x16A085,
        "defaultSize": [0.8, 1.5, 0.8],
        "model": "disconnector",
        "label": "隔离开关",
    },
    # 电流互感器：环形 + 支柱
    "current_transformer": {
        "icon": "📊",
        "color": 0xD68910,
        "defaultSize": [0.6, 2, 0.6],
        "model": "ct",
        "label": "电流互感器",
    },
    # 电压互感器：圆筒形 + 支柱
    "potential_transformer": {
        "icon": "📈",
        "color": 0x7D3C98,
        "defaultSize": [0.6, 2, 0.6],
        "model": "pt",
        "label": "电压互感器",
    },
    # 避雷器：多节圆柱堆叠 + 顶部球
    "lightning_arrester": {
        "icon": "🌩️",
        "color": 0x1ABC9C,
        "defaultSize": [0.4, 2.5, 0.4],
        "model": "arrester",
        "label": "避雷器",
    },
    # 母线：长条 + 支柱绝缘子
    "busbar": {
        "icon": "➖",
        "color": 0x95A5A6,
        "defaultSize": [8, 0.3, 0.3],
        "model": "busbar",
        "label": "母线",
    },
    # 电缆：弯曲管道
    "cable": {
        "icon": "📡",
        "color": 0x34495E,
        "defaultSize": [0.3, 0.3, 6],
        "model": "cable",
        "label": "电缆",
    },
    # 补偿装置（电抗器/电容器组）：大方块 + 散热鳍
    "compensation": {
        "icon": "⚙️",
        "color": 0x2874A6,
        "defaultSize": [2, 2, 2],
        "model": "compensation",
        "label": "补偿装置",
    },
    # 直流/电源系统：机柜造型
    "powersupply": {
        "icon": "🔋",
        "color": 0x117A65,
        "defaultSize": [1.5, 2, 1.5],
        "model": "powersupply",
        "label": "电源系统",
    },
}


def _load_layout(station_id: str) -> dict:
    """从 JSON 文件加载站布局模板（带缓存）。"""
    if station_id in _layout_cache:
        return _layout_cache[station_id]
    layout_path = settings.TWIN_LAYOUT_PATH
    if not os.path.isabs(layout_path):
        # 相对于 backend/ 目录
        backend_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        layout_path = os.path.join(backend_root, layout_path)
    try:
        with open(layout_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _layout_cache[station_id] = data
        return data
    except Exception as e:
        degraded("twin_load_layout", e)
        return {"stationId": station_id, "stationName": station_id, "areas": [], "devices": []}


def _color_by_risk(risk_score: float) -> str:
    """riskScore → HSL 色带（绿→黄→红）。

    riskScore 0-5: 绿色（低风险）
    riskScore 5-10: 黄色（中风险）
    riskScore 10+: 红色（高风险）
    返回 CSS color 字符串供前端使用。
    """
    if risk_score <= 0:
        return "#2ECC71"  # 绿色（正常）
    if risk_score < 5:
        # 绿→黄渐变
        t = risk_score / 5.0
        h = 120 - int(60 * t)  # 120(绿) → 60(黄)
        return f"hsl({h}, 70%, 50%)"
    if risk_score < 10:
        # 黄→橙
        t = (risk_score - 5) / 5.0
        h = 60 - int(30 * t)  # 60(黄) → 30(橙)
        return f"hsl({h}, 80%, 50%)"
    # 橙→红
    t = min(1.0, (risk_score - 10) / 10.0)
    h = 30 - int(30 * t)  # 30(橙) → 0(红)
    return f"hsl({h}, 90%, 50%)"


async def get_station_layout(station_id: str = "110kV-demo") -> dict:
    """获取站点布局模板（areas + devices + connections）。"""
    return _load_layout(station_id)


async def get_station_overview(station_id: str = "110kV-demo") -> dict:
    """获取站点总览：设备列表 + 各设备状态（riskScore/颜色/告警/工单）。

    聚合 fault_prediction_service + alert_disposal_service + ticket 数据。
    """
    layout = _load_layout(station_id)
    devices = layout.get("devices", [])

    # 获取风险预测数据
    risk_map: dict[str, dict] = {}
    try:
        from app.services.fault_prediction_service import predict
        pred = await predict(days=30)
        for item in pred.get("items", []):
            title = item.get("title", "")
            # 模糊匹配：risk item title 匹配 device name 或 kgEntity
            for dev in devices:
                dev_name = dev.get("name", "")
                kg_entity = dev.get("kgEntity", "")
                if title and (title in dev_name or dev_name in title or
                              title in kg_entity or kg_entity in title):
                    risk_map[dev["deviceId"]] = item
                    break
    except Exception as e:
        degraded("twin_predict", e)

    # 构建设备状态列表
    device_statuses: list[dict] = []
    for dev in devices:
        dev_id = dev["deviceId"]
        risk_item = risk_map.get(dev_id, {})
        risk_score = risk_item.get("riskScore", 0)
        risk_level = risk_item.get("riskLevel", "低")
        color = _color_by_risk(risk_score)
        blink = risk_score >= 10  # 高风险闪烁
        type_meta = DEVICE_TYPES.get(dev.get("type", ""), {})
        # 兼容旧 JSON 中 type="disconnector" 的电抗/电容/电源设备 → 走 compensation/powersupply 模型
        actual_model = type_meta.get("model", "default")
        if dev.get("type") == "disconnector":
            # 根据 kgEntity 名字识别具体类型
            kg = dev.get("kgEntity", "")
            if "电抗" in kg or "电容" in kg:
                actual_model = "compensation"
            elif "直流" in kg or "UPS" in kg or "电源" in kg:
                actual_model = "powersupply"
        device_statuses.append({
            "deviceId": dev_id,
            "name": dev.get("name", ""),
            "type": dev.get("type", ""),
            "position": dev.get("position", [0, 0, 0]),
            "size": dev.get("size", type_meta.get("defaultSize", [1, 1, 1])),
            "area": dev.get("area", ""),
            "kgEntity": dev.get("kgEntity", ""),
            "connections": dev.get("connections", []),
            "riskScore": risk_score,
            "riskLevel": risk_level,
            "alertStatus": "active" if blink else "normal",
            "color": color,
            "blink": blink,
            "icon": type_meta.get("icon", "📦"),
            "model": actual_model,  # 前端按此选择几何体工厂
            "typeLabel": type_meta.get("label", "设备"),
        })

    areas = layout.get("areas", [])
    return {
        "stationId": layout.get("stationId", station_id),
        "stationName": layout.get("stationName", ""),
        "voltageLevel": layout.get("voltageLevel", ""),
        "type": layout.get("type", "outdoor"),
        "areas": areas,
        "devices": device_statuses,
        "deviceCount": len(device_statuses),
        "highRiskCount": sum(1 for d in device_statuses if d["blink"]),
        "generatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


async def get_device_detail(device_id: str) -> dict:
    """获取设备详情：风险/告警/知识图谱上下文/故障传播链/工单。

    聚合 kg_service.graph_context + kg_service.get_paths + fault_prediction。
    """
    layout = _load_layout("110kV-demo")
    devices = layout.get("devices", [])
    dev = next((d for d in devices if d["deviceId"] == device_id), None)
    if not dev:
        return {"error": "设备不存在", "deviceId": device_id}

    kg_entity = dev.get("kgEntity", dev.get("name", ""))
    detail: dict[str, Any] = {
        "deviceId": device_id,
        "name": dev.get("name", ""),
        "type": dev.get("type", ""),
        "area": dev.get("area", ""),
        "position": dev.get("position", [0, 0, 0]),
        "kgEntity": kg_entity,
        "connections": dev.get("connections", []),
    }

    # 1. 知识图谱上下文
    try:
        from app.services.kg_service import graph_context
        with trace_span("twin.kg_context"):
            kg_rows = await graph_context(kg_entity, 8)
        detail["kgContext"] = kg_rows
    except Exception as e:
        degraded("twin_kg_context", e)
        detail["kgContext"] = []

    # 2. 故障传播链（多跳路径）
    try:
        from app.services.kg_service import get_paths
        with trace_span("twin.fault_chain"):
            paths = await get_paths(kg_entity, depth=3, limit=10)
        detail["faultChain"] = paths
    except Exception as e:
        degraded("twin_fault_chain", e)
        detail["faultChain"] = []

    # 3. 风险评分
    try:
        from app.services.fault_prediction_service import predict
        pred = await predict(days=30)
        risk_item = None
        for item in pred.get("items", []):
            title = item.get("title", "")
            if title and (title in dev.get("name", "") or dev.get("name", "") in title or
                          title in kg_entity or kg_entity in title):
                risk_item = item
                break
        if risk_item:
            detail["riskScore"] = risk_item.get("riskScore", 0)
            detail["riskLevel"] = risk_item.get("riskLevel", "低")
            detail["suggestion"] = risk_item.get("suggestion", "")
            detail["color"] = _color_by_risk(risk_item.get("riskScore", 0))
        else:
            detail["riskScore"] = 0
            detail["riskLevel"] = "低"
            detail["color"] = _color_by_risk(0)
    except Exception as e:
        degraded("twin_risk", e)
        detail["riskScore"] = 0
        detail["riskLevel"] = "低"
        detail["color"] = "#2ECC71"

    # 4. 告警状态
    try:
        from app.services.alert_disposal_service import list_disposals
        disposals = await list_disposals(page=1, size=5)
        related = []
        for d in disposals.get("list", []):
            title = d.get("title", "")
            if title and (kg_entity in title or title in kg_entity or
                          dev.get("name", "") in title or title in dev.get("name", "")):
                related.append(d)
        detail["alerts"] = related
    except Exception as e:
        degraded("twin_alerts", e)
        detail["alerts"] = []

    detail["traceId"] = get_trace_id()
    return detail


async def get_fault_chain(device_id: str, depth: int = 3) -> list[dict]:
    """获取设备故障传播链（kg_service.get_paths 封装）。"""
    layout = _load_layout("110kV-demo")
    devices = layout.get("devices", [])
    dev = next((d for d in devices if d["deviceId"] == device_id), None)
    if not dev:
        return []
    kg_entity = dev.get("kgEntity", dev.get("name", ""))
    try:
        from app.services.kg_service import get_paths
        return await get_paths(kg_entity, depth=depth, limit=20)
    except Exception as e:
        degraded("twin_get_fault_chain", e)
        return []


async def push_alert_location(alert: dict) -> dict:
    """告警定位推送：查设备坐标 → WebSocket 广播给孪生前端。

    alert: {severity, title, device/deviceId, ...}
    返回推送结果。
    """
    layout = _load_layout("110kV-demo")
    devices = layout.get("devices", [])

    # 匹配设备
    device_id = alert.get("deviceId", "")
    device_name = alert.get("device", alert.get("title", ""))
    dev = None
    if device_id:
        dev = next((d for d in devices if d["deviceId"] == device_id), None)
    if not dev:
        # 模糊匹配 name/kgEntity
        for d in devices:
            kg = d.get("kgEntity", "")
            nm = d.get("name", "")
            if device_name and (device_name in nm or nm in device_name or
                                device_name in kg or kg in device_name):
                dev = d
                break

    if not dev:
        return {"matched": False, "message": "未匹配到设备"}

    position = dev.get("position", [0, 0, 0])
    message = {
        "type": "alert",
        "deviceId": dev["deviceId"],
        "deviceName": dev.get("name", ""),
        "position": position,
        "severity": alert.get("severity", "warning"),
        "title": alert.get("title", ""),
        "summary": alert.get("summary", ""),
        "timestamp": datetime.datetime.now().isoformat(),
        "traceId": get_trace_id(),
    }

    # WebSocket 广播
    try:
        from app.core.ws_manager import broadcast_twin
        await broadcast_twin(message)
    except Exception as e:
        degraded("twin_push_alert", e)

    return {"matched": True, "deviceId": dev["deviceId"], "position": position, "pushed": True}
