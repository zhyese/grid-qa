"""N2 MCP 工具总线模块。

双向能力：
- Server (server.py)：把本系统 6 个能力暴露为 MCP tools，供外部 Agent 调用
- Client (client.py)：发现外部 MCP server → schema 转换 → 注册进 ToolRegistry
- Registry (registry.py)：配置驱动的 server 列表管理
"""
