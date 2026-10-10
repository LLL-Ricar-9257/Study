"""宠物医院 MCP 服务。

基于官方 Python SDK ``mcp==2.0.0`` 的 ``MCPServer``，对外暴露无状态
Streamable HTTP（协议版本 ``2026-07-28``），把 Go 宠物医院 REST API
包装成 AI Agent 可调用的工具。
"""

from __future__ import annotations

from .config import Settings
from .errors import (
    ErrorCode,
    ErrorInfo,
    PetHospitalMCPError,
    ToolErrorOutput,
)
from .rest_client import PetHospitalRestClient
from .server import PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION, build_app, create_server, main

__version__ = SERVER_VERSION

__all__ = [
    "__version__",
    "Settings",
    "ErrorCode",
    "ErrorInfo",
    "ToolErrorOutput",
    "PetHospitalMCPError",
    "PetHospitalRestClient",
    "SERVER_NAME",
    "SERVER_VERSION",
    "PROTOCOL_VERSION",
    "create_server",
    "build_app",
    "main",
]