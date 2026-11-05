"""MCP Client 的配置、命名映射、结果格式化与端到端测试。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.agent import FileAgent
from core.bootstrap import build_agent
from core.config import AgentLimits, AppConfig, LLMSettings
from core.errors import ConfigurationError
from core.events import ToolFinished
from core.mcp.config import load_mcp_config
from core.mcp.manager import McpClientManager, McpSettings
from core.mcp.tools import build_bindings, format_result
from core.models import ChatChunk, ToolCallDelta
from core.tools.registry import ToolRegistry

pytest.importorskip("mcp")

_SERVER_SCRIPT = '''\
try:
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

server = _Server("xagent-test")


@server.tool()
def echo(text: str) -> str:
    """Echo the given text."""
    return f"echo:{text}"


@server.tool()
def structured() -> dict:
    """Return structured data."""
    return {"status": "ok", "value": 42}


@server.tool()
def boom() -> str:
    """Always fail."""
    raise RuntimeError("kaboom")


if __name__ == "__main__":
    server.run("stdio")
'''


def _write_config(root: Path, servers: dict[str, object]) -> Path:
    path = root / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": servers}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _tool(
    name: str,
    *,
    description: str = "demo tool",
    schema: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=description,
        input_schema=schema or {"type": "object", "properties": {}},
    )


@pytest.fixture()
def stdio_project(tmp_path: Path) -> Path:
    script = tmp_path / "demo_server.py"
    script.write_text(_SERVER_SCRIPT, encoding="utf-8")
    _write_config(
        tmp_path,
        {
            "demo": {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(script)],
                "cwd": ".",
            },
            "broken": {
                "type": "stdio",
                "command": "xagent-definitely-missing-binary",
            },
        },
    )
    return tmp_path


class _FakeModel:
    """第一轮请求 MCP 工具，收到 tool 消息后给出最终回答。"""

    def __init__(self, tool_name: str, arguments: dict[str, object]) -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.seen_tool_names: tuple[str, ...] = ()

    async def stream(self, messages, tools):
        self.seen_tool_names = tuple(
            item["function"]["name"] for item in tools if item.get("type") == "function"
        )
        if messages[-1].role == "tool":
            yield ChatChunk(text="已完成 MCP 调用")
            return
        yield ChatChunk(
            tool_call_deltas=(
                ToolCallDelta(
                    index=0,
                    id="call_mcp_1",
                    name=self.tool_name,
                    arguments=json.dumps(self.arguments),
                ),
            )
        )

    async def aclose(self) -> None:
        return None


# -- 配置解析 ---------------------------------------------------------------


def test_load_config_supports_both_transports(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEMO_TOKEN", "secret-token")
    path = _write_config(
        tmp_path,
        {
            "local": {
                "type": "stdio",
                "command": "python",
                "args": ["-m", "demo", "--token", "${DEMO_TOKEN}"],
                "env": {"DEMO_MODE": "1"},
                "cwd": "sub",
            },
            "remote": {
                "type": "streamable_http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${DEMO_TOKEN}"},
            },
        },
    )

    config = load_mcp_config(tmp_path)

    assert config.path == path
    assert config.issues == ()
    local, remote = config.servers
    assert local.transport == "stdio"
    assert local.command == "python"
    assert local.args == ("-m", "demo", "--token", "secret-token")
    assert dict(local.env) == {"DEMO_MODE": "1"}
    assert local.cwd == (tmp_path / "sub").resolve()
    assert remote.transport == "streamable_http"
    assert remote.url == "https://example.com/mcp"
    assert dict(remote.headers) == {"Authorization": "Bearer secret-token"}
    assert "secret-token" not in repr(remote)


def test_type_is_inferred_when_omitted(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "local": {"command": "python", "args": ["server.py"]},
            "remote": {"url": "https://example.com/mcp"},
        },
    )
    config = load_mcp_config(tmp_path)
    assert [server.transport for server in config.servers] == [
        "stdio",
        "streamable_http",
    ]


def test_missing_config_file_is_not_an_error(tmp_path: Path) -> None:
    config = load_mcp_config(tmp_path)
    assert config.path is None
    assert config.servers == ()
    assert config.issues == ()
    assert config.empty


def test_invalid_json_raises(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_mcp_config(tmp_path)


def test_missing_environment_variable_skips_only_that_server(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("XAGENT_TEST_MISSING_TOKEN", raising=False)
    _write_config(
        tmp_path,
        {
            "bad": {
                "type": "streamable_http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${XAGENT_TEST_MISSING_TOKEN}"},
            },
            "good": {
                "type": "streamable_http",
                "url": "https://example.com/other",
            },
        },
    )

    config = load_mcp_config(tmp_path)

    assert [server.name for server in config.servers] == ["good"]
    assert len(config.issues) == 1
    assert "XAGENT_TEST_MISSING_TOKEN" in config.issues[0]


def test_unsupported_transport_is_reported_as_issue(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "legacy": {"type": "sse", "url": "https://example.com/sse"},
            "good": {"url": "https://example.com/mcp"},
        },
    )

    config = load_mcp_config(tmp_path)

    assert [server.name for server in config.servers] == ["good"]
    assert "sse" in config.issues[0]


def test_explicit_config_path_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_mcp_config(tmp_path, path="missing.json")


# -- 工具命名与 Schema ------------------------------------------------------


def test_tool_names_use_server_prefix_and_avoid_conflicts() -> None:
    used: set[str] = set()
    first, issues = build_bindings(
        "Bazi-MCP",
        [_tool("getBaziDetail")],
        used=used,
    )
    second, _ = build_bindings(
        "Bazi-MCP",
        [_tool("getBaziDetail")],
        used=used,
    )

    assert issues == []
    assert first[0].exposed_name == "Bazi-MCP__getBaziDetail"
    assert second[0].exposed_name == "Bazi-MCP__getBaziDetail_2"
    assert first[0].server_name == "Bazi-MCP"
    assert first[0].tool_name == "getBaziDetail"
    assert "Bazi-MCP" in first[0].spec.description


def test_tool_names_respect_local_reserved_names_and_length_limit() -> None:
    long_name = "x" * 120
    bindings, _ = build_bindings(
        "服务",
        [_tool(long_name)],
        reserved={"mcp_server__" + long_name[:48], "Runcil"},
    )

    exposed = bindings[0].exposed_name
    assert len(exposed) <= 64
    assert exposed.isascii()
    assert exposed.casefold() != "runcil"


def test_parameters_schema_is_sanitized_and_invalid_root_is_skipped() -> None:
    bindings, issues = build_bindings(
        "demo",
        [
            _tool(
                "with_title",
                schema={
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            ),
            _tool("scalar", schema={"type": "string"}),
        ],
    )

    assert [binding.tool_name for binding in bindings] == ["with_title"]
    parameters = bindings[0].spec.parameters
    assert "$schema" not in parameters
    assert parameters["type"] == "object"
    assert parameters["required"] == ["name"]
    assert issues and "scalar" in issues[0]


# -- 结果格式化 -------------------------------------------------------------


def test_format_result_collects_text_and_structured_content() -> None:
    result = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="第一段"),
            SimpleNamespace(type="text", text="第二段"),
        ],
        structured_content={"status": "ok", "value": 42},
        is_error=False,
    )

    text, is_error = format_result(result, server_name="demo", tool_name="t")

    assert not is_error
    assert "第一段" in text and "第二段" in text
    assert "结构化数据" in text
    assert '"value": 42' in text


def test_format_result_marks_tool_errors() -> None:
    result = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="参数 gender 缺失")],
        structured_content=None,
        is_error=True,
    )

    text, is_error = format_result(result, server_name="Bazi-MCP", tool_name="getBaziDetail")

    assert is_error
    assert "Bazi-MCP" in text
    assert "参数 gender 缺失" in text


def test_format_result_handles_images_and_empty_results() -> None:
    image_result = SimpleNamespace(
        content=[
            SimpleNamespace(type="image", mime_type="image/png", data="aGVsbG8=")
        ],
        structured_content=None,
        is_error=False,
    )
    text, _ = format_result(image_result, server_name="demo", tool_name="image")
    assert "图片内容" in text and "image/png" in text and "aGVsbG8=" not in text

    empty_text, _ = format_result(
        SimpleNamespace(content=[], structured_content=None, is_error=False),
        server_name="demo",
        tool_name="empty",
    )
    assert "空结果" in empty_text


def test_format_result_truncates_long_output() -> None:
    result = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="x" * 500)],
        structured_content=None,
        is_error=False,
    )
    text, _ = format_result(
        result,
        server_name="demo",
        tool_name="long",
        max_chars=100,
    )
    assert "截断" in text
    assert len(text) < 500


# -- 端到端：stdio 连接、注册、调用、错误隔离 --------------------------------


def test_stdio_end_to_end_and_failure_isolation(stdio_project: Path) -> None:
    manager = McpClientManager.from_project(
        stdio_project,
        settings=McpSettings(connect_timeout=30, call_timeout=30),
    )
    assert manager is not None
    manager.start()
    try:
        summaries = {summary.name: summary for summary in manager.summaries}
        assert summaries["demo"].connected
        assert summaries["demo"].tool_count >= 3
        assert not summaries["broken"].connected
        assert "broken" in " ".join(manager.notices())

        names = {spec.name for spec in manager.tool_specs()}
        assert {"demo__echo", "demo__structured", "demo__boom"} <= names

        registry = ToolRegistry(manager.registry_handlers(), manager.tool_specs())

        echoed = registry.execute("demo__echo", json.dumps({"text": "你好"}))
        assert echoed.ok
        assert "echo:你好" in echoed.content

        structured = registry.execute("demo__structured", "{}")
        assert structured.ok
        assert "42" in structured.content

        failed = registry.execute("demo__boom", "{}")
        assert not failed.ok
        assert "boom" in failed.content
        assert "返回错误" in failed.content

        unknown = registry.execute("demo__missing", "{}")
        assert not unknown.ok
    finally:
        manager.close()


def test_build_agent_registers_mcp_tools_and_reports_status(
    stdio_project: Path,
) -> None:
    config = AppConfig(
        workspace=stdio_project,
        llm=LLMSettings(api_key="test-key"),
        limits=AgentLimits(),
    )

    agent = build_agent(
        config,
        project_root=stdio_project,
        bundled_roots=(),
        mcp_settings=McpSettings(connect_timeout=30, call_timeout=30),
    )
    try:
        prompt = agent.messages[0].content
        assert "MCP" in prompt
        assert "demo" in prompt
        assert "broken" in prompt
        notices = " ".join(agent.startup_notices)
        assert "demo" in notices
        assert "broken" in notices
        assert any(spec.name == "demo__echo" for spec in agent.tool_specs)
    finally:
        asyncio.run(agent.aclose())


def test_agent_loop_forwards_mcp_function_call(stdio_project: Path) -> None:
    manager = McpClientManager.from_project(
        stdio_project,
        settings=McpSettings(connect_timeout=30, call_timeout=30),
    )
    assert manager is not None
    manager.start()

    model = _FakeModel("demo__echo", {"text": "loop"})
    agent = FileAgent(
        model=model,
        tools=ToolRegistry(manager.registry_handlers(), manager.tool_specs()),
        system_prompt="test",
        cleanup=[manager.close],
    )

    async def collect() -> list[object]:
        return [event async for event in agent.send("调用 MCP 工具")]

    try:
        events = asyncio.run(collect())
        assert "demo__echo" in model.seen_tool_names
        finished = [event for event in events if isinstance(event, ToolFinished)]
        assert len(finished) == 1
        assert finished[0].ok
        assert "echo:loop" in finished[0].content
    finally:
        asyncio.run(agent.aclose())


# -- 真实远程服务器（手动开启） ---------------------------------------------

_LIVE_CONFIG = Path(
    os.getenv(
        "XAGENT_MCP_CONFIG",
        str(Path(__file__).resolve().parents[3] / ".mcp.json"),
    )
)


@pytest.mark.skipif(
    os.getenv("XAGENT_MCP_LIVE_TEST") != "1",
    reason="设置 XAGENT_MCP_LIVE_TEST=1 后运行真实 MCP Server 测试",
)
def test_live_streamable_http_server() -> None:
    if not _LIVE_CONFIG.is_file():
        pytest.skip(f"缺少 MCP 配置文件：{_LIVE_CONFIG}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copy2(_LIVE_CONFIG, root / ".mcp.json")
        manager = McpClientManager.from_project(
            root,
            settings=McpSettings(connect_timeout=60, call_timeout=60),
        )
        assert manager is not None
        manager.start()
        try:
            specs = manager.tool_specs()
            assert specs, manager.notices()
            registry = ToolRegistry(manager.registry_handlers(), specs)
            target = next(
                (spec for spec in specs if "getChineseCalendar" in spec.name),
                specs[0],
            )
            arguments = (
                {"solarDatetime": "2024-02-10T12:00:00+08:00"}
                if "getChineseCalendar" in target.name
                else {}
            )
            result = registry.execute(target.name, json.dumps(arguments))
            assert result.ok, result.content
            assert result.content.strip()
        finally:
            manager.close()
