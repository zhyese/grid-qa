"""N2 MCP 工具总线测试。

测试重点：
- mcp_to_openai / openai_to_mcp schema 转换 1:1
- mock_scada_server query_telemetry 返回正确结构
- mcp_client discover → register_tools → call_tool 链路（用 mock）
- mcp_registry 配置驱动加载
"""
import asyncio
import json

import pytest
from httpx import Response

from app.mcp.client import McpClient, mcp_to_openai, openai_to_mcp
from app.mcp.registry import McpRegistry, McpServerConfig
from app.mcp import mock_scada_server
from app.services.agent_runtime import Tool, ToolRegistry


# ===== schema 转换 1:1 =====
def test_mcp_to_openai_basic():
    """MCP tool schema -> OpenAI tool schema 基本转换。"""
    mcp_tool = {
        "name": "query_telemetry",
        "description": "查询设备遥测数据",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string", "description": "设备ID"},
            },
            "required": ["device_id"],
        },
    }
    oai = mcp_to_openai(mcp_tool)
    assert oai["type"] == "function"
    assert oai["function"]["name"] == "query_telemetry"
    assert oai["function"]["description"] == "查询设备遥测数据"
    assert oai["function"]["parameters"] == mcp_tool["inputSchema"]


def test_openai_to_mcp_basic():
    """OpenAI tool schema -> MCP tool schema 基本转换。"""
    oai_tool = {
        "type": "function",
        "function": {
            "name": "search_regulation",
            "description": "搜索规程",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }
    mcp = openai_to_mcp(oai_tool)
    assert mcp["name"] == "search_regulation"
    assert mcp["description"] == "搜索规程"
    assert mcp["inputSchema"] == oai_tool["function"]["parameters"]


def test_mcp_to_openai_roundtrip():
    """MCP -> OpenAI -> MCP 往返保持一致。"""
    original_mcp = {
        "name": "test_tool",
        "description": "测试工具",
        "inputSchema": {"type": "object", "properties": {"x": {"type": "integer"}}},
    }
    oai = mcp_to_openai(original_mcp)
    back = openai_to_mcp(oai)
    assert back == original_mcp


def test_openai_to_mcp_roundtrip():
    """OpenAI -> MCP -> OpenAI 往返保持一致。"""
    original_oai = {
        "type": "function",
        "function": {
            "name": "roundtrip",
            "description": "往返测试",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    mcp = openai_to_mcp(original_oai)
    back = mcp_to_openai(mcp)
    assert back == original_oai


def test_mcp_to_openai_missing_fields_defaults():
    """MCP tool 缺少字段时使用默认值。"""
    oai = mcp_to_openai({})
    assert oai["type"] == "function"
    assert oai["function"]["name"] == ""
    assert oai["function"]["description"] == ""
    assert oai["function"]["parameters"] == {"type": "object", "properties": {}}


def test_openai_to_mcp_missing_function_defaults():
    """OpenAI tool 缺少 function 时使用默认值。"""
    mcp = openai_to_mcp({})
    assert mcp["name"] == ""
    assert mcp["description"] == ""
    assert mcp["inputSchema"] == {"type": "object", "properties": {}}


# ===== mock_scada_server 测试 =====
def test_mock_scada_tools_structure():
    """mock_scada_server MOCK_TOOLS 结构正确。"""
    tools = mock_scada_server.MOCK_TOOLS
    assert len(tools) == 1
    tool = tools[0]
    assert tool["name"] == "query_telemetry"
    assert "description" in tool
    assert "inputSchema" in tool
    assert tool["inputSchema"]["required"] == ["device_id"]


def test_gen_telemetry_returns_correct_fields():
    """_gen_telemetry 返回正确的遥测字段结构。"""
    data = mock_scada_server._gen_telemetry("T1_main_transformer")
    expected_keys = {"deviceId", "deviceName", "deviceType", "voltage", "current",
                     "power", "temperature", "frequency", "status", "timestamp"}
    assert set(data.keys()) == expected_keys
    assert data["deviceId"] == "T1_main_transformer"
    assert data["deviceName"] == "1号主变压器"
    assert data["status"] == "running"
    # T1_main_transformer 不含 110kV/10kV，base_voltage=35.0
    assert 33.0 <= data["voltage"] <= 37.0


def test_gen_telemetry_110kv_device_voltage():
    """110kV 设备的电压基础值为 110.0。"""
    data = mock_scada_server._gen_telemetry("line_110kV_01_CB")
    assert 108.0 <= data["voltage"] <= 112.0


def test_gen_telemetry_unknown_device():
    """未知设备也能生成遥测数据（name=device_id）。"""
    data = mock_scada_server._gen_telemetry("unknown_device")
    assert data["deviceId"] == "unknown_device"
    assert data["deviceName"] == "unknown_device"
    assert data["deviceType"] == "unknown"


def test_format_telemetry_contains_all_fields():
    """_format_telemetry 格式化文本包含所有遥测字段。"""
    data = mock_scada_server._gen_telemetry("T1_main_transformer")
    text = mock_scada_server._format_telemetry(data)
    assert "设备:" in text
    assert "电压:" in text
    assert "电流:" in text
    assert "有功功率:" in text
    assert "温度:" in text
    assert "频率:" in text
    assert "状态:" in text


def test_mock_scada_list_tools_endpoint():
    """mock_scada_server /mcp/tools/list 端点返回 tools 列表。"""
    from starlette.testclient import TestClient
    client = TestClient(mock_scada_server.app)
    resp = client.get("/mcp/tools/list")
    assert resp.status_code == 200
    data = resp.json()
    assert "tools" in data
    assert len(data["tools"]) == 1
    assert data["tools"][0]["name"] == "query_telemetry"


def test_mock_scada_call_tool_endpoint():
    """mock_scada_server /mcp/tools/call 端点返回遥测数据。"""
    from starlette.testclient import TestClient
    client = TestClient(mock_scada_server.app)
    resp = client.post("/mcp/tools/call", json={
        "name": "query_telemetry",
        "arguments": {"device_id": "T1_main_transformer"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data
    assert "1号主变压器" in data["result"]
    assert "电压:" in data["result"]


def test_mock_scada_call_tool_missing_device_id():
    """缺少 device_id 参数返回提示。"""
    from starlette.testclient import TestClient
    client = TestClient(mock_scada_server.app)
    resp = client.post("/mcp/tools/call", json={
        "name": "query_telemetry",
        "arguments": {},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "缺少" in data["result"]


def test_mock_scada_call_unknown_tool():
    """调用未知工具返回提示。"""
    from starlette.testclient import TestClient
    client = TestClient(mock_scada_server.app)
    resp = client.post("/mcp/tools/call", json={
        "name": "nonexistent",
        "arguments": {},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "未知工具" in data["result"]


def test_mock_scada_health_endpoint():
    """mock_scada /health 端点返回 ok。"""
    from starlette.testclient import TestClient
    client = TestClient(mock_scada_server.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ===== McpRegistry 配置驱动加载 =====
def test_mcp_registry_load_from_config(monkeypatch):
    """从 JSON 配置加载 server 列表。"""
    from app.config import settings
    monkeypatch.setattr(settings, "MCP_SERVERS",
                        json.dumps([{"name": "mock_scada", "url": "http://localhost:9100"}]))
    reg = McpRegistry()
    count = asyncio.run(reg.load_from_config())
    assert count == 1
    srv = reg.get_server("mock_scada")
    assert srv is not None
    assert srv.url == "http://localhost:9100"
    assert srv.enabled is True


def test_mcp_registry_empty_config(monkeypatch):
    """空配置加载 0 个 server。"""
    from app.config import settings
    monkeypatch.setattr(settings, "MCP_SERVERS", "")
    reg = McpRegistry()
    count = asyncio.run(reg.load_from_config())
    assert count == 0


def test_mcp_registry_invalid_json(monkeypatch):
    """无效 JSON 配置加载 0 个 server。"""
    from app.config import settings
    monkeypatch.setattr(settings, "MCP_SERVERS", "not-json")
    reg = McpRegistry()
    count = asyncio.run(reg.load_from_config())
    assert count == 0


def test_mcp_registry_list_enabled():
    """list_enabled 只返回 enabled=True 的 server。"""
    reg = McpRegistry()
    reg._servers["a"] = McpServerConfig(name="a", url="http://a", enabled=True)
    reg._servers["b"] = McpServerConfig(name="b", url="http://b", enabled=False)
    enabled = reg.list_enabled()
    assert len(enabled) == 1
    assert enabled[0].name == "a"


def test_mcp_registry_update_tools():
    """update_tools 更新 server 的 tools 列表。"""
    reg = McpRegistry()
    reg._servers["s1"] = McpServerConfig(name="s1", url="http://s1")
    reg.update_tools("s1", [{"name": "tool1"}])
    assert len(reg._servers["s1"].tools) == 1


def test_mcp_registry_list_all_tools():
    """list_all_tools 扁平化返回所有 enabled server 的 tools。"""
    reg = McpRegistry()
    reg._servers["a"] = McpServerConfig(name="a", url="http://a", enabled=True,
                                        tools=[{"name": "t1"}, {"name": "t2"}])
    reg._servers["b"] = McpServerConfig(name="b", url="http://b", enabled=False,
                                        tools=[{"name": "t3"}])
    all_tools = reg.list_all_tools()
    assert len(all_tools) == 2
    assert all(t["_server"] == "a" for t in all_tools)


# ===== McpClient discover/register/call 链路（mock httpx） =====
def test_mcp_client_discover_success(monkeypatch):
    """discover 成功获取工具列表。"""
    client = McpClient()

    class FakeAsyncClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kw):
            return Response(200, json={"tools": [{"name": "query_telemetry", "description": "d",
                                                   "inputSchema": {"type": "object"}}]})

    monkeypatch.setattr("app.mcp.client.httpx.AsyncClient", FakeAsyncClient)
    servers = [McpServerConfig(name="mock_scada", url="http://localhost:9100")]
    results = asyncio.run(client.discover(servers))
    assert len(results) == 1
    assert results[0]["server"] == "mock_scada"
    assert len(results[0]["tools"]) == 1


def test_mcp_client_discover_skips_disabled():
    """discover 跳过 enabled=False 的 server。"""
    client = McpClient()
    servers = [McpServerConfig(name="disabled_srv", url="http://x", enabled=False)]
    results = asyncio.run(client.discover(servers))
    assert len(results) == 0


def test_mcp_client_discover_unreachable_server(monkeypatch):
    """不可达的 server 被跳过，不抛异常。"""
    client = McpClient()

    class FakeAsyncClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kw):
            raise ConnectionError("unreachable")

    monkeypatch.setattr("app.mcp.client.httpx.AsyncClient", FakeAsyncClient)
    servers = [McpServerConfig(name="dead", url="http://dead:9100")]
    results = asyncio.run(client.discover(servers))
    assert len(results) == 0  # 异常被 degraded 吞掉


def test_mcp_client_register_tools():
    """register_tools 把外部工具注册进 ToolRegistry。"""
    client = McpClient()
    registry = ToolRegistry()
    discovered = [{
        "server": "mock_scada",
        "url": "http://localhost:9100",
        "tools": [{
            "name": "query_telemetry",
            "description": "查询遥测",
            "inputSchema": {"type": "object", "properties": {"device_id": {"type": "string"}}},
        }],
    }]
    count = client.register_tools(registry, discovered)
    assert count == 1
    tool = registry.get("query_telemetry")
    assert tool is not None
    assert tool.description == "查询遥测"


def test_mcp_client_register_tools_avoids_name_conflict():
    """与内置工具重名时自动加前缀。"""
    client = McpClient()
    registry = ToolRegistry()
    # 预注册一个同名内置工具
    async def builtin_handler(db, mt, **a):
        return "builtin"
    registry.register(Tool(name="query_telemetry", description="内置",
                           parameters={}, handler=builtin_handler))
    discovered = [{
        "server": "mock_scada",
        "url": "http://localhost:9100",
        "tools": [{"name": "query_telemetry", "description": "外部",
                    "inputSchema": {"type": "object"}}],
    }]
    count = client.register_tools(registry, discovered)
    assert count == 1
    # 外部工具被重命名为 mcp_mock_scada_query_telemetry
    assert registry.get("mcp_mock_scada_query_telemetry") is not None


def test_mcp_client_call_tool_success(monkeypatch):
    """call_tool 成功调用并返回结果文本。"""
    client = McpClient()

    class FakeAsyncClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kw):
            assert kw["headers"]["X-Tenant-Id"] == "tenant-a"
            return Response(200, json={"result": "电压: 110kV"})

    monkeypatch.setattr("app.mcp.client.httpx.AsyncClient", FakeAsyncClient)
    result = asyncio.run(client.call_tool("http://localhost:9100", "query_telemetry",
                                           {"device_id": "T1"}, tenant_id="tenant-a"))
    assert "110kV" in result


def test_mcp_client_call_tool_content_format(monkeypatch):
    """call_tool 处理 MCP content 格式返回。"""
    client = McpClient()

    class FakeAsyncClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kw):
            return Response(200, json={"content": [{"type": "text", "text": "结果A"},
                                                    {"type": "text", "text": "结果B"}]})

    monkeypatch.setattr("app.mcp.client.httpx.AsyncClient", FakeAsyncClient)
    result = asyncio.run(client.call_tool("http://x", "t", {}))
    assert "结果A" in result and "结果B" in result


def test_mcp_client_call_tool_http_error(monkeypatch):
    """call_tool HTTP 错误返回失败提示。"""
    client = McpClient()

    class FakeAsyncClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kw):
            return Response(500)

    monkeypatch.setattr("app.mcp.client.httpx.AsyncClient", FakeAsyncClient)
    result = asyncio.run(client.call_tool("http://x", "t", {}))
    assert "失败" in result


def test_mcp_client_call_tool_exception(monkeypatch):
    """call_tool 网络异常返回异常提示。"""
    client = McpClient()

    class FakeAsyncClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kw):
            raise ConnectionError("network down")

    monkeypatch.setattr("app.mcp.client.httpx.AsyncClient", FakeAsyncClient)
    result = asyncio.run(client.call_tool("http://x", "t", {}))
    assert "异常" in result
