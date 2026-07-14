"""N2 Mock SCADA MCP Server：示例外部 MCP server，提供设备遥测查询。

作为独立进程运行（python -m app.mcp.mock_scada_server），验证 MCP client
发现→注册→调用链路完整性。真实 SCADA 接入时替换 handler 即可。

提供工具：
- query_telemetry(device_id): 返回模拟遥测数据（电压/电流/温度/功率）
"""
import datetime
import random

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Mock SCADA MCP Server", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 模拟设备遥测数据
_MOCK_DEVICES: dict[str, dict] = {
    "T1_main_transformer": {"name": "1号主变压器", "type": "main_transformer"},
    "T2_main_transformer": {"name": "2号主变压器", "type": "main_transformer"},
    "line_110kV_01_CB": {"name": "110kV出线01", "type": "transmission_line"},
    "10kV_feeder_01_CB": {"name": "10kV出线01", "type": "feeder"},
}

MOCK_TOOLS: list[dict] = [
    {
        "name": "query_telemetry",
        "description": "查询设备实时遥测数据（电压/电流/温度/功率），模拟 SCADA 接口。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string", "description": "设备 ID，如 T1_main_transformer"},
            },
            "required": ["device_id"],
        },
    },
]


def _gen_telemetry(device_id: str) -> dict:
    """生成模拟遥测数据（带随机波动）。"""
    dev = _MOCK_DEVICES.get(device_id, {"name": device_id, "type": "unknown"})
    base_voltage = 110.0 if "110kV" in device_id else 10.5 if "10kV" in device_id else 35.0
    return {
        "deviceId": device_id,
        "deviceName": dev["name"],
        "deviceType": dev["type"],
        "voltage": round(base_voltage + random.uniform(-2, 2), 2),  # kV
        "current": round(random.uniform(100, 500), 2),  # A
        "power": round(random.uniform(1, 20), 2),  # MW
        "temperature": round(random.uniform(35, 75), 1),  # °C
        "frequency": round(50.0 + random.uniform(-0.2, 0.2), 2),  # Hz
        "status": "running",
        "timestamp": datetime.datetime.now().isoformat(),
    }


def _format_telemetry(data: dict) -> str:
    """格式化遥测数据为 LLM 可读文本。"""
    return (
        f"设备: {data['deviceName']} ({data['deviceId']})\n"
        f"电压: {data['voltage']} kV\n"
        f"电流: {data['current']} A\n"
        f"有功功率: {data['power']} MW\n"
        f"温度: {data['temperature']} °C\n"
        f"频率: {data['frequency']} Hz\n"
        f"状态: {data['status']}\n"
        f"时间: {data['timestamp']}"
    )


@app.get("/mcp/tools/list")
async def list_tools():
    """列出 mock server 提供的 MCP tools。"""
    return {"tools": MOCK_TOOLS}


@app.post("/mcp/tools/list")
async def list_tools_post():
    """MCP HTTP 传输协议：POST list_tools。"""
    return {"tools": MOCK_TOOLS}


@app.post("/mcp/tools/call")
async def call_tool(request: Request):
    """调用 mock MCP tool。

    Body: {"name": "query_telemetry", "arguments": {"device_id": "T1_main_transformer"}}
    """
    body = await request.json()
    name = body.get("name", "")
    args = body.get("arguments", {}) or {}

    if name == "query_telemetry":
        device_id = args.get("device_id", "")
        if not device_id:
            return {"result": "缺少 device_id 参数"}
        data = _gen_telemetry(device_id)
        return {"result": _format_telemetry(data)}
    else:
        return {"result": f"未知工具: {name}"}


@app.get("/health")
async def health():
    """健康检查。"""
    return {"status": "ok", "server": "mock_scada"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9100)
