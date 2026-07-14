"""N2 MCP Client：发现外部 MCP server → schema 转换 → 注册进 ToolRegistry → 调用。

工作流：
1. discover(servers)：遍历配置的 MCP server，调 list_tools 获取工具列表
2. register_tools(registry)：把外部工具 schema 转换为 OpenAI 格式，注册进 ToolRegistry
3. call_tool(server_url, tool_name, args)：调用外部 MCP server 的工具

schema 转换 1:1（MCP inputSchema == OpenAI parameters）：
  MCP → OpenAI: {"type":"function","function":{"name":mcp.name,"description":mcp.desc,"parameters":mcp.inputSchema}}
  OpenAI → MCP: {"name":oai.function.name,"description":oai.function.description,"inputSchema":oai.function.parameters}
"""
import json
from typing import Any

import httpx

from app.core.obs import degraded
from app.services.agent_runtime import Tool
from app.mcp.registry import McpServerConfig


def mcp_to_openai(mcp_tool: dict) -> dict:
    """MCP tool schema → OpenAI tool schema（1:1 映射）。"""
    return {
        "type": "function",
        "function": {
            "name": mcp_tool.get("name", ""),
            "description": mcp_tool.get("description", ""),
            "parameters": mcp_tool.get("inputSchema", {"type": "object", "properties": {}}),
        },
    }


def openai_to_mcp(oai_tool: dict) -> dict:
    """OpenAI tool schema → MCP tool schema（1:1 映射）。"""
    fn = oai_tool.get("function", {})
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "inputSchema": fn.get("parameters", {"type": "object", "properties": {}}),
    }


class McpClient:
    """MCP Client：发现/注册/调用外部 MCP server 工具。"""

    async def discover(self, servers: list[McpServerConfig]) -> list[dict]:
        """遍历 MCP server，调 list_tools 获取工具列表。

        Returns: [{"server": name, "url": url, "tools": [mcp_tool_schema, ...]}, ...]
        """
        results: list[dict] = []
        async with httpx.AsyncClient(timeout=10) as client:
            for srv in servers:
                if not srv.enabled:
                    continue
                try:
                    resp = await client.post(
                        f"{srv.url}/mcp/tools/list",
                        headers=self._auth_headers(srv.token),
                        json={},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        tools = data.get("tools", []) if isinstance(data, dict) else []
                        results.append({"server": srv.name, "url": srv.url, "tools": tools})
                    else:
                        degraded("mcp_discover", Exception(f"server {srv.name} returned {resp.status_code}"))
                except Exception as e:
                    degraded("mcp_discover", e, f"server {srv.name} 不可达")
        return results

    def register_tools(self, registry, discovered: list[dict]) -> int:
        """把发现的外部工具注册进 ToolRegistry。

        每个外部工具包装为 Tool，handler 统一调 _mcp_tool_handler。
        Returns: 注册的工具数量
        """
        count = 0
        for item in discovered:
            server_name = item.get("server", "")
            server_url = item.get("url", "")
            for mcp_tool in item.get("tools", []):
                tool_name = mcp_tool.get("name", "")
                if not tool_name:
                    continue
                # 避免与内置工具重名
                if registry.get(tool_name):
                    tool_name = f"mcp_{server_name}_{tool_name}"

                oai_schema = mcp_to_openai(mcp_tool)
                fn = oai_schema["function"]
                parameters = fn["parameters"]

                # 创建闭包捕获 server_url 和 tool_name
                def make_handler(srv_url, t_name, srv_token=""):
                    async def handler(db, model_type, **args):
                        return await self.call_tool(srv_url, t_name, args, srv_token)
                    return handler

                tool = Tool(
                    name=tool_name,
                    description=fn.get("description", f"MCP tool: {tool_name}"),
                    parameters=parameters,
                    handler=make_handler(server_url, tool_name, ""),
                )
                registry.register(tool)
                count += 1
        return count

    async def call_tool(self, server_url: str, tool_name: str,
                        args: dict, token: str = "") -> str:
        """调用外部 MCP server 的工具。

        Returns: 工具结果文本（LLM 可读）
        """
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(
                    f"{server_url}/mcp/tools/call",
                    headers=self._auth_headers(token),
                    json={"name": tool_name, "arguments": args or {}},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # MCP 返回 {result: str} 或 {content: [{type: text, text: str}]}
                    if isinstance(data, dict):
                        if "result" in data:
                            return str(data["result"])
                        if "content" in data:
                            texts = [c.get("text", "") for c in data["content"] if isinstance(c, dict)]
                            return "\n".join(texts) or "工具返回空结果"
                    return json.dumps(data, ensure_ascii=False)
                else:
                    return f"工具调用失败: HTTP {resp.status_code}"
            except Exception as e:
                degraded("mcp_call_tool", e)
                return f"工具调用异常: {type(e).__name__}: {e}"

    def _auth_headers(self, token: str) -> dict:
        """构建鉴权 header。"""
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers


# 单例
mcp_client = McpClient()
