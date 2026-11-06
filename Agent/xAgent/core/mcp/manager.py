"""MCP 客户端管理器：连接服务器、发现工具、转发调用。

设计要点：

- MCP Python SDK 是异步 API，而 xAgent 的 ``ToolRegistry`` 以同步 handler 运行在
  ``asyncio.to_thread`` 工作线程中。管理器因此拥有一个专用的后台事件循环线程，
  在启动阶段完成 ``initialize()`` / ``list_tools()``，之后每次工具调用通过
  ``asyncio.run_coroutine_threadsafe`` 转发到该循环，并同步等待结果。
- 每个服务器的连接（``stdio`` 子进程或 HTTP 会话）由同一事件循环中的常驻任务
  持有，直到 ``close()``；不同服务器互不影响。
- 连接失败只记录在 ``summaries`` / ``issues`` 中，不抛异常、不影响本地工具。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import os
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError, ToolError
from ..tools.types import ToolSpec
from .config import McpConfig, McpServerConfig, load_mcp_config
from .tools import (
    McpToolBinding,
    build_bindings,
    format_result,
    sanitize_name_token,
)

_DEFAULT_CONNECT_TIMEOUT = 20.0
_DEFAULT_CALL_TIMEOUT = 60.0
_DEFAULT_MAX_RESULT_CHARS = 30_000
_DEFAULT_SHUTDOWN_TIMEOUT = 8.0


@dataclass(frozen=True, slots=True)
class McpSettings:
    """MCP 运行时开关与超时，可通过环境变量覆盖。"""

    enabled: bool = True
    config_path: str | None = None
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT
    call_timeout: float = _DEFAULT_CALL_TIMEOUT
    max_result_chars: int = _DEFAULT_MAX_RESULT_CHARS
    shutdown_timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT

    def __post_init__(self) -> None:
        if self.connect_timeout <= 0:
            raise ConfigurationError("MCP connect_timeout 必须大于 0")
        if self.call_timeout <= 0:
            raise ConfigurationError("MCP call_timeout 必须大于 0")
        if self.max_result_chars < 1:
            raise ConfigurationError("MCP max_result_chars 必须大于 0")
        if self.shutdown_timeout <= 0:
            raise ConfigurationError("MCP shutdown_timeout 必须大于 0")

    @classmethod
    def from_environment(cls) -> McpSettings:
        """读取 ``XAGENT_MCP_*`` 环境变量；非法数值回退到默认值。"""
        raw_enabled = os.getenv("XAGENT_MCP_ENABLED", "").strip().casefold()
        enabled = raw_enabled not in {"0", "false", "no", "off", "disabled"}
        return cls(
            enabled=enabled,
            config_path=os.getenv("XAGENT_MCP_CONFIG", "").strip() or None,
            connect_timeout=_env_float(
                "XAGENT_MCP_CONNECT_TIMEOUT", _DEFAULT_CONNECT_TIMEOUT
            ),
            call_timeout=_env_float("XAGENT_MCP_CALL_TIMEOUT", _DEFAULT_CALL_TIMEOUT),
            max_result_chars=int(
                _env_float(
                    "XAGENT_MCP_MAX_RESULT_CHARS",
                    float(_DEFAULT_MAX_RESULT_CHARS),
                )
            ),
            shutdown_timeout=_env_float(
                "XAGENT_MCP_SHUTDOWN_TIMEOUT", _DEFAULT_SHUTDOWN_TIMEOUT
            ),
        )


@dataclass(frozen=True, slots=True)
class McpServerSummary:
    """供系统提示词、UI 启动提示使用的服务器状态。"""

    name: str
    transport: str
    endpoint: str
    connected: bool
    tool_count: int = 0
    tool_names: tuple[str, ...] = ()
    error: str = ""
    prefix: str = ""


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _shorten(text: str, limit: int = 600) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + "……"


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_timeout(sub) for sub in exc.exceptions)
    return False


def _describe_exception(exc: BaseException) -> str:
    """展开 anyio TaskGroup 的 ExceptionGroup，返回可读的错误描述。"""
    if isinstance(exc, BaseExceptionGroup):
        details: list[str] = []
        for sub in exc.exceptions:
            text = _describe_exception(sub)
            if text and text not in details:
                details.append(text)
        if details:
            return "；".join(details)
    message = str(exc)
    if not message:
        return type(exc).__name__
    return f"{type(exc).__name__}: {message}"


def _read_open_tail(handle: Any, limit: int = 800) -> str:
    """读取 stderr 临时文件的末尾，用于连接失败诊断。"""
    try:
        handle.flush()
        position = handle.tell()
        handle.seek(0)
        text = handle.read()
        handle.seek(position)
    except (OSError, ValueError):
        return ""
    text = " ".join(str(text).split())
    if len(text) > limit:
        text = "……" + text[-limit:]
    return text


class McpClientManager:
    """管理所有 MCP Server 的连接、工具映射与调用。"""

    def __init__(
        self,
        config: McpConfig,
        *,
        settings: McpSettings | None = None,
        project_root: str | Path | None = None,
        reserved_names: Iterable[str] = (),
    ) -> None:
        self._config = config
        self._settings = settings or McpSettings()
        self._project_root = Path(project_root or Path.cwd()).expanduser().resolve()
        self._reserved_names = tuple(reserved_names)

        self._issues: list[str] = list(config.issues)
        self._failures: dict[str, str] = {}
        self._discovered: dict[str, tuple[Any, ...]] = {}
        self._sessions: dict[str, Any] = {}
        self._summaries: tuple[McpServerSummary, ...] = ()
        self._bindings: tuple[McpToolBinding, ...] = ()
        self._routes: dict[str, McpToolBinding] = {}
        self._startup_error = ""

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._ready: dict[str, threading.Event] = {}
        self._serve_tasks: list[asyncio.Task[Any]] = []
        self._stop_event: asyncio.Event | None = None
        self._started = False
        self._closed = False
        self._lock = threading.Lock()

    # -- 构造 -------------------------------------------------------------

    @classmethod
    def from_project(
        cls,
        project_root: str | Path,
        *,
        settings: McpSettings | None = None,
        reserved_names: Iterable[str] = (),
    ) -> McpClientManager | None:
        """读取项目目录的 ``.mcp.json`` 并创建管理器。

        没有配置文件或通过 ``XAGENT_MCP_ENABLED=0`` 关闭时返回 ``None``；
        配置文件损坏时仍返回管理器，错误会出现在启动提示与系统提示词中。
        """
        effective = settings or McpSettings.from_environment()
        if not effective.enabled:
            return None

        root = Path(project_root).expanduser().resolve()
        try:
            config = load_mcp_config(root, path=effective.config_path)
        except ConfigurationError as exc:
            config = McpConfig(issues=(str(exc),))

        if config.empty:
            return None
        return cls(
            config,
            settings=effective,
            project_root=root,
            reserved_names=reserved_names,
        )

    # -- 只读属性 ---------------------------------------------------------

    @property
    def config(self) -> McpConfig:
        return self._config

    @property
    def summaries(self) -> tuple[McpServerSummary, ...]:
        return self._summaries

    @property
    def issues(self) -> tuple[str, ...]:
        return tuple(self._issues)

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """已连接服务器的工具声明；未连接的服务器不会出现在这里。"""
        return tuple(binding.spec for binding in self._bindings)

    # -- 启动 -------------------------------------------------------------

    def start(self) -> None:
        """连接所有服务器并发现工具。阻塞到连接/超时，但不会抛异常。"""
        if self._started or self._closed:
            return
        self._started = True

        if not self._config.servers:
            self._build_bindings()
            return

        try:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="xagent-mcp",
                daemon=True,
            )
            self._thread.start()
            if not self._loop_ready.wait(timeout=5):
                raise RuntimeError("MCP 客户端事件循环启动超时")

            self._ready = {
                server.name: threading.Event()
                for server in self._config.servers
            }
            future = asyncio.run_coroutine_threadsafe(self._serve_all(), self._loop)
            future.result(timeout=5)
        except Exception as exc:
            self._startup_error = _shorten(
                f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            )
            self._issues.append(f"MCP 客户端启动失败：{self._startup_error}")
            self._build_bindings()
            return

        limit = max(self._settings.connect_timeout, 1.0) + 10.0
        for server in self._config.servers:
            ready = self._ready.get(server.name)
            if ready is None:
                continue
            if not ready.wait(timeout=limit):
                self._failures.setdefault(server.name, "连接超时")

        self._build_bindings()

    def _run_loop(self) -> None:
        loop = self._loop
        if loop is None:  # pragma: no cover - 防御性分支
            return
        asyncio.set_event_loop(loop)
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    async def _serve_all(self) -> None:
        self._stop_event = asyncio.Event()
        self._serve_tasks = [
            asyncio.create_task(self._serve_one(server))
            for server in self._config.servers
        ]

    async def _serve_one(self, server: McpServerConfig) -> None:
        ready = self._ready[server.name]
        errlog = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        try:
            async with contextlib.AsyncExitStack() as stack:
                async with asyncio.timeout(self._settings.connect_timeout):
                    read_stream, write_stream = await self._open_transport(
                        stack, server, errlog
                    )
                    from mcp import ClientSession

                    session = await stack.enter_async_context(
                        ClientSession(read_stream, write_stream)
                    )
                    await session.initialize()
                    listed = await session.list_tools()

                self._sessions[server.name] = session
                self._discovered[server.name] = tuple(listed.tools)
                ready.set()
                assert self._stop_event is not None
                await self._stop_event.wait()
        except asyncio.CancelledError:
            ready.set()
            raise
        except Exception as exc:
            if _is_timeout(exc):
                detail = (
                    f"连接或工具发现超时（超过 "
                    f"{self._settings.connect_timeout:g} 秒）"
                )
            elif isinstance(exc, ImportError):
                detail = (
                    "缺少 MCP Python SDK（mcp），请先运行 "
                    f"pip install mcp：{exc}"
                )
            else:
                detail = _shorten(_describe_exception(exc))
            tail = _read_open_tail(errlog)
            if tail:
                detail = f"{detail}；服务器 stderr 末尾：{tail}"
            if ready.is_set():
                self._issues.append(f"服务器 {server.name} 连接异常结束：{detail}")
            else:
                self._failures[server.name] = detail
                ready.set()
        finally:
            with contextlib.suppress(Exception):
                errlog.close()

    async def _open_transport(
        self,
        stack: contextlib.AsyncExitStack,
        server: McpServerConfig,
        errlog: Any,
    ) -> tuple[Any, Any]:
        if server.transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            parameters: dict[str, Any] = {
                "command": server.command,
                "args": list(server.args),
                "cwd": str(server.cwd or self._project_root),
            }
            if server.env:
                parameters["env"] = {**os.environ, **dict(server.env)}
            params = StdioServerParameters(**parameters)
            if "errlog" in inspect.signature(stdio_client).parameters:
                streams = await stack.enter_async_context(
                    stdio_client(params, errlog=errlog)
                )
            else:  # pragma: no cover - 旧版 SDK
                streams = await stack.enter_async_context(stdio_client(params))
            return streams[0], streams[1]

        module = importlib.import_module("mcp.client.streamable_http")
        open_transport = getattr(module, "streamable_http_client", None) or getattr(
            module, "streamablehttp_client", None
        )
        if open_transport is None:  # pragma: no cover - 依赖版本异常
            raise RuntimeError("当前 mcp SDK 版本不支持 Streamable HTTP 传输")

        parameters = inspect.signature(open_transport).parameters
        kwargs = {}
        if "http_client" in parameters:
            # mcp 2.x：请求头通过自定义 httpx 客户端传入。
            factory = getattr(module, "create_mcp_http_client", None)
            if factory is None:  # pragma: no cover - 依赖版本异常
                raise RuntimeError("当前 mcp SDK 缺少 create_mcp_http_client")
            client = factory(headers=dict(server.headers) or None)
            await stack.enter_async_context(client)
            kwargs["http_client"] = client
        elif server.headers:
            # mcp 1.x：streamablehttp_client(url, headers=...)
            kwargs["headers"] = dict(server.headers)

        streams = await stack.enter_async_context(
            open_transport(server.url, **kwargs)
        )
        return streams[0], streams[1]

    def _build_bindings(self) -> None:
        used = {name.casefold() for name in self._reserved_names}
        bindings: list[McpToolBinding] = []
        summaries: list[McpServerSummary] = []

        for server in self._config.servers:
            endpoint = server.endpoint
            if server.name in self._failures:
                summaries.append(
                    McpServerSummary(
                        name=server.name,
                        transport=server.transport,
                        endpoint=endpoint,
                        connected=False,
                        error=self._failures[server.name],
                    )
                )
                continue

            tools = self._discovered.get(server.name)
            if tools is None:
                summaries.append(
                    McpServerSummary(
                        name=server.name,
                        transport=server.transport,
                        endpoint=endpoint,
                        connected=False,
                        error=self._startup_error or "连接未完成",
                    )
                )
                continue

            server_bindings, issues = build_bindings(
                server.name,
                tools,
                used=used,
            )
            self._issues.extend(f"{server.name}：{issue}" for issue in issues)
            bindings.extend(server_bindings)
            summaries.append(
                McpServerSummary(
                    name=server.name,
                    transport=server.transport,
                    endpoint=endpoint,
                    connected=True,
                    tool_count=len(server_bindings),
                    tool_names=tuple(
                        binding.exposed_name for binding in server_bindings
                    ),
                    prefix=sanitize_name_token(server.name, "mcp_server"),
                )
            )

        self._bindings = tuple(bindings)
        self._routes = {binding.exposed_name: binding for binding in bindings}
        self._summaries = tuple(summaries)

    # -- 调用 -------------------------------------------------------------

    def registry_handlers(self) -> dict[str, Callable[..., str]]:
        """返回 对外工具名 -> 同步 handler，供 ``ToolRegistry`` 注册。"""
        handlers: dict[str, Callable[..., str]] = {}
        for binding in self._bindings:
            handlers[binding.exposed_name] = self._make_handler(binding)
        return handlers

    def _make_handler(self, binding: McpToolBinding) -> Callable[..., str]:
        def handler(**arguments: Any) -> str:
            return self.call_tool(binding.exposed_name, arguments)

        handler.__name__ = f"mcp_{binding.exposed_name}"
        handler.__doc__ = binding.spec.description
        return handler

    def call_tool(self, exposed_name: str, arguments: Mapping[str, Any]) -> str:
        """同步调用 MCP 工具；失败转换为 ``ToolError`` 回传给模型。"""
        binding = self._routes.get(exposed_name)
        if binding is None:
            raise ToolError(f"未知的 MCP 工具：{exposed_name}")

        session = self._sessions.get(binding.server_name)
        loop = self._loop
        if session is None or loop is None:
            reason = self._failures.get(
                binding.server_name,
                self._startup_error or "尚未建立连接",
            )
            raise ToolError(
                f"MCP 服务器「{binding.server_name}」不可用：{reason}"
            )

        if not isinstance(arguments, Mapping):
            raise ToolError("MCP 工具参数必须是 JSON 对象")

        future = asyncio.run_coroutine_threadsafe(
            session.call_tool(binding.tool_name, dict(arguments)),
            loop,
        )
        try:
            result = future.result(timeout=self._settings.call_timeout)
        except TimeoutError as exc:
            future.cancel()
            raise ToolError(
                f"MCP 工具「{binding.server_name}/{binding.tool_name}」调用超时"
                f"（超过 {self._settings.call_timeout:g} 秒）"
            ) from exc
        except Exception as exc:
            raise ToolError(
                f"MCP 工具「{binding.server_name}/{binding.tool_name}」调用失败："
                f"{_shorten(_describe_exception(exc))}"
            ) from exc

        text, is_error = format_result(
            result,
            server_name=binding.server_name,
            tool_name=binding.tool_name,
            max_chars=self._settings.max_result_chars,
        )
        if is_error:
            raise ToolError(text)
        return text

    # -- 状态与关闭 -------------------------------------------------------

    def prompt_lines(self) -> tuple[str, ...]:
        """系统提示词中的 MCP 段落（每项一行，不含编号）。"""
        lines: list[str] = []
        if self._config.path is not None:
            lines.append(f"配置文件：{self._config.path}")
        for summary in self._summaries:
            if summary.connected:
                lines.append(
                    f"- MCP 服务器「{summary.name}」（{summary.transport}）："
                    f"已连接，注册 {summary.tool_count} 个工具，"
                    f"工具名以「{summary.prefix}__」开头。"
                )
            else:
                lines.append(
                    f"- MCP 服务器「{summary.name}」（{summary.transport}）："
                    f"连接失败：{summary.error}"
                )
        for issue in self._issues:
            lines.append(f"- MCP 配置提示：{issue}")
        return tuple(lines)

    def notices(self) -> tuple[str, ...]:
        """启动时展示给用户的状态/错误提示。"""
        lines: list[str] = []
        if self._config.path is not None:
            lines.append(f"MCP：已读取配置 {self._config.path}")
        for summary in self._summaries:
            if summary.connected:
                lines.append(
                    f"MCP 服务器「{summary.name}」已连接，"
                    f"注册 {summary.tool_count} 个工具（前缀 {summary.prefix}__）。"
                )
            else:
                lines.append(
                    f"MCP 服务器「{summary.name}」连接失败：{summary.error}"
                )
        for issue in self._issues:
            lines.append(f"MCP 配置问题：{issue}")
        return tuple(lines)

    def close(self) -> None:
        """停止事件循环并关闭所有 MCP 会话；可重复调用。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True

        loop = self._loop
        if loop is None:
            return

        if self._thread is None or not self._thread.is_alive():
            with contextlib.suppress(Exception):
                if not loop.is_running():
                    loop.close()
            return

        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            future.result(timeout=self._settings.shutdown_timeout + 5)
        except Exception:
            pass
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(loop.stop)
        self._thread.join(timeout=self._settings.shutdown_timeout + 5)

    async def _shutdown(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        tasks = [task for task in self._serve_tasks if not task.done()]
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self._settings.shutdown_timeout,
            )
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
