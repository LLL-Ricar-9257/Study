"""MCP 服务装配：``MCPServer`` + 无状态 Streamable HTTP。

本模块是本服务唯一的 SDK 2.x 装配点，明确遵守以下约束：

* 使用 ``mcp.server.MCPServer``，**不使用** ``mcp.server.fastmcp``；
* 协议版本 ``2026-07-28``（``mcp.types.LATEST_PROTOCOL_VERSION``）；
* ``streamable_http_app(stateless_http=True, json_response=True, event_store=None)``：
  每个 POST 独立处理，**不产生也不接受 ``Mcp-Session-Id``**，
  没有会话存储、没有会话过期、没有 ``max_sessions``、没有有状态 SSE 断点续传；
* 客户端在无状态模式下**无需发送 ``initialize``**，直接 ``tools/list`` /
  ``tools/call`` 即可，请求以 ``MCP-Protocol-Version`` 头 + ``params._meta``
  信封声明协议版本；
* 教学场景不启用认证、权限、CORS / Origin 校验，因此显式关闭 DNS rebinding 防护。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import Settings
from .errors import InternalError
from .logging_config import configure_logging
from .rest_client import PetHospitalRestClient
from .tools import register_tools

__all__ = [
    "SERVER_NAME",
    "SERVER_VERSION",
    "PROTOCOL_VERSION",
    "create_server",
    "build_app",
    "main",
]

logger = logging.getLogger(__name__)

SERVER_NAME = "pet-hospital-mcp"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = LATEST_PROTOCOL_VERSION

INSTRUCTIONS = """\
本服务把宠物医院 REST API 暴露给 AI Agent，当前阶段只提供一个只读工具 `list_pets`
（对应 `GET /api/v1/pets`）。

调用方式：直接向 `/mcp` 发起 POST，携带 `MCP-Protocol-Version: 2026-07-28` 与
`Mcp-Method` 头，请求体 `params._meta` 里放
`io.modelcontextprotocol/protocolVersion` 与 `io.modelcontextprotocol/clientCapabilities`。
服务是无状态的，不需要 `initialize`，也不会返回 `Mcp-Session-Id`。

失败时工具结果为 `isError: true`，文本内容是统一错误结构
`{"error": {"code", "message", "details"}}`。
"""


def create_server(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    client: PetHospitalRestClient | None = None,
) -> MCPServer:
    """构造 ``MCPServer``：注册工具 + ``/health``，并接管 REST 客户端生命周期。"""
    rest_client = client or PetHospitalRestClient(settings, transport=transport)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        logger.info(
            "server_starting",
            extra={
                "server_name": SERVER_NAME,
                "server_version": SERVER_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "backend": settings.pet_hospital_base_url,
            },
        )
        try:
            yield None
        finally:
            await rest_client.aclose()

    server = MCPServer(
        name=SERVER_NAME,
        title="宠物医院 MCP 服务",
        version=SERVER_VERSION,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )

    register_tools(server, rest_client)
    _register_health_route(server, settings)
    return server


def _register_health_route(server: MCPServer, settings: Settings) -> None:
    """挂载 ``GET /health``。

    只报告 MCP 服务自身状态，不去打后端——Go 服务停掉时 MCP 仍应算健康，
    否则容器编排会把一个"只是下游不可用"的服务反复重启。
    """

    @server.custom_route("/health", methods=["GET"], name="health")
    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "healthy",
                "server": SERVER_NAME,
                "version": SERVER_VERSION,
                "protocolVersion": PROTOCOL_VERSION,
                "sdk": "mcp==2.0.0",
                "transport": "streamable-http (stateless)",
                "backend": settings.pet_hospital_base_url,
            }
        )


def build_app(
    settings: Settings,
    *,
    server: MCPServer | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    client: PetHospitalRestClient | None = None,
) -> Starlette:
    """返回可直接交给 uvicorn 的 Starlette 应用。"""
    mcp_server = server or create_server(settings, transport=transport, client=client)
    return mcp_server.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        json_response=True,
        stateless_http=True,
        event_store=None,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host=settings.host,
    )


def main() -> None:
    """命令行入口：``python -m pet_hospital_mcp``。"""
    import uvicorn

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    app = build_app(settings)
    logger.info(
        "mcp_endpoint",
        extra={
            "url": f"http://{settings.host}:{settings.port}{settings.mcp_path}",
            "health": f"http://{settings.host}:{settings.port}/health",
        },
    )
    try:
        uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())
    except Exception as exc:  # pragma: no cover - 仅用于把启动期异常转成统一结构打印
        logger.error("startup_failed", extra={"error": InternalError(details={"reason": type(exc).__name__}).to_output().model_dump()})
        raise


def health_routes(app: Starlette) -> list[Route]:
    """返回 app 上的非 MCP 路由（测试与调试用）。"""
    return [route for route in app.routes if isinstance(route, Route) and route.path != "/mcp"]