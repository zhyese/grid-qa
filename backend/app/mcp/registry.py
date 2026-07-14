"""N2 MCP Server 注册表：配置驱动的外部 MCP server 列表管理。

从 settings.MCP_SERVERS（JSON）加载 server 配置，供 mcp_client 发现和调用。
"""
import json
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings


@dataclass
class McpServerConfig:
    """外部 MCP server 配置。"""
    name: str
    url: str
    token: str = ""
    enabled: bool = True
    tools: list[dict] = field(default_factory=list)  # 发现后填充


class McpRegistry:
    """MCP server 注册表单例。"""

    def __init__(self):
        self._servers: dict[str, McpServerConfig] = {}

    async def load_from_config(self) -> int:
        """从 settings.MCP_SERVERS JSON 加载 server 配置。

        格式：[{"name":"mock_scada","url":"http://localhost:9100","token":"xxx"}]
        Returns: 加载的 server 数量
        """
        raw = settings.MCP_SERVERS
        if not raw or not raw.strip():
            return 0
        try:
            servers = json.loads(raw)
        except Exception:
            return 0

        count = 0
        for srv in servers:
            if not isinstance(srv, dict):
                continue
            name = srv.get("name", "").strip()
            url = srv.get("url", "").strip()
            if not name or not url:
                continue
            config = McpServerConfig(
                name=name,
                url=url,
                token=srv.get("token", ""),
                enabled=srv.get("enabled", True),
            )
            self._servers[name] = config
            count += 1
        return count

    def get_server(self, name: str) -> Optional[McpServerConfig]:
        """按名称获取 server 配置。"""
        return self._servers.get(name)

    def list_servers(self) -> list[McpServerConfig]:
        """列出所有已加载的 server 配置。"""
        return list(self._servers.values())

    def list_enabled(self) -> list[McpServerConfig]:
        """列出所有启用的 server。"""
        return [s for s in self._servers.values() if s.enabled]

    def server_count(self) -> int:
        """已加载的 server 总数。"""
        return len(self._servers)

    def update_tools(self, name: str, tools: list[dict]) -> None:
        """更新 server 发现的 tools 列表。"""
        srv = self._servers.get(name)
        if srv:
            srv.tools = tools

    def list_all_tools(self) -> list[dict]:
        """列出所有 server 的所有 tools（扁平化）。"""
        out: list[dict] = []
        for srv in self._servers.values():
            if not srv.enabled:
                continue
            for tool in srv.tools:
                out.append({**tool, "_server": srv.name, "_server_url": srv.url})
        return out


# 单例
mcp_registry = McpRegistry()
