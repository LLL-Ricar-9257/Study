"""MCP Client：连接外部 MCP Server，并把其工具注册为本地 Function Calling 工具。

公共入口：

- ``load_mcp_config(project_root)``：读取当前项目目录下的 ``.mcp.json``；
- ``parse_server_entry(...)``：校验单个服务器配置，``createMcp`` 写文件前复用；
- ``McpClientManager.from_project(...)``：连接服务器、发现工具、转发调用。

本包只在真正建立连接时才 ``import mcp``，因此未安装 MCP SDK 时
xAgent 仍可正常启动，只是 MCP 工具不可用。
"""

from .config import (
    CONFIG_FILENAME,
    McpConfig,
    McpServerConfig,
    discover_config_path,
    load_mcp_config,
    parse_server_entry,
)
from .manager import McpClientManager, McpServerSummary, McpSettings
from .tools import McpToolBinding, build_bindings, format_result

__all__ = [
    "CONFIG_FILENAME",
    "McpClientManager",
    "McpConfig",
    "McpServerConfig",
    "McpServerSummary",
    "McpSettings",
    "McpToolBinding",
    "build_bindings",
    "discover_config_path",
    "format_result",
    "load_mcp_config",
    "parse_server_entry",
]
