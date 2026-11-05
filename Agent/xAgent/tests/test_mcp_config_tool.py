"""``createMcp`` 工具：写入、合并、校验、覆盖保护与注册测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.bootstrap import build_agent
from core.config import AgentLimits, AppConfig, LLMSettings
from core.errors import ToolError
from core.mcp.config import load_mcp_config
from core.mcp.manager import McpSettings
from core.tools.mcpconfig import McpConfigToolset
from core.tools.registry import ToolRegistry
from core.tools.specs import MCP_CONFIG_TOOL_SPECS


def _toolset(root: Path, **kwargs) -> McpConfigToolset:
    return McpConfigToolset(project_root=root, **kwargs)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


STDIO = {"type": "stdio", "command": "python", "args": ["server.py"]}
REMOTE = {
    "type": "streamable_http",
    "url": "https://example.com/mcp",
    "headers": {"Authorization": "Bearer ${DEMO_TOKEN}"},
}


# -- 新建与合并 -------------------------------------------------------------


def test_creates_config_file_with_both_transports(tmp_path: Path) -> None:
    target = tmp_path / ".mcp.json"
    assert not target.exists()

    result = _toolset(tmp_path).create_mcp({"demo": STDIO, "remote": REMOTE})

    assert "已写入 MCP 配置" in result
    assert "demo" in result and "remote" in result
    payload = _read(target)
    assert payload["mcpServers"]["demo"] == STDIO
    assert payload["mcpServers"]["remote"] == REMOTE
    assert target.read_text(encoding="utf-8").endswith("\n")
    # 写入结果能被运行期解析器正确读取（环境变量缺失只影响展开，不影响文件内容）
    assert set(_read(target)["mcpServers"]) == {"demo", "remote"}


def test_merges_with_existing_servers_and_keeps_other_fields(tmp_path: Path) -> None:
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {"keep": {"url": "https://example.com/keep"}},
                "comment": "手工维护的字段",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _toolset(tmp_path).create_mcp({"added": STDIO})

    payload = _read(target)
    assert set(payload["mcpServers"]) == {"keep", "added"}
    assert payload["comment"] == "手工维护的字段"
    assert "keep" in result


def test_legacy_key_is_updated_in_place(tmp_path: Path) -> None:
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps({"mcp_servers": {"old": {"url": "https://example.com/old"}}}),
        encoding="utf-8",
    )

    _toolset(tmp_path).create_mcp({"new": STDIO})

    payload = _read(target)
    assert "mcpServers" not in payload
    assert set(payload["mcp_servers"]) == {"old", "new"}


def test_empty_file_is_initialized(tmp_path: Path) -> None:
    target = tmp_path / ".mcp.json"
    target.write_text("   \n", encoding="utf-8")

    _toolset(tmp_path).create_mcp({"demo": STDIO})

    assert set(_read(target)["mcpServers"]) == {"demo"}


def test_json_string_is_accepted(tmp_path: Path) -> None:
    result = _toolset(tmp_path).create_mcp(json.dumps({"demo": STDIO}))
    assert "demo" in result
    assert "demo" in _read(tmp_path / ".mcp.json")["mcpServers"]


# -- 覆盖保护 ---------------------------------------------------------------


def test_existing_server_is_not_overwritten_by_default(tmp_path: Path) -> None:
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps({"mcpServers": {"demo": {"url": "https://example.com/old"}}}),
        encoding="utf-8",
    )
    before = target.read_text(encoding="utf-8")

    with pytest.raises(ToolError) as excinfo:
        _toolset(tmp_path).create_mcp({"demo": STDIO})

    assert "overwrite=true" in str(excinfo.value)
    assert target.read_text(encoding="utf-8") == before


def test_overwrite_replaces_existing_server(tmp_path: Path) -> None:
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps({"mcpServers": {"demo": {"url": "https://example.com/old"}}}),
        encoding="utf-8",
    )

    result = _toolset(tmp_path).create_mcp({"demo": STDIO}, overwrite=True)

    assert "覆盖服务器" in result
    assert _read(target)["mcpServers"]["demo"] == STDIO


# -- 校验与文件保护 ---------------------------------------------------------


@pytest.mark.parametrize(
    "servers",
    [
        {"bad": {"type": "sse", "url": "https://example.com/sse"}},
        {"bad": {"type": "stdio"}},
        {"bad": {"type": "stdio", "command": "python", "args": "server.py"}},
        {"bad": {"url": "ftp://example.com/mcp"}},
        {"bad": {"env": {"A": "1"}}},
    ],
)
def test_invalid_server_config_writes_nothing(tmp_path: Path, servers: dict) -> None:
    with pytest.raises(ToolError):
        _toolset(tmp_path).create_mcp(servers)
    assert not (tmp_path / ".mcp.json").exists()


def test_invalid_server_does_not_modify_existing_file(tmp_path: Path) -> None:
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps({"mcpServers": {"keep": {"url": "https://example.com/k"}}}),
        encoding="utf-8",
    )
    before = target.read_text(encoding="utf-8")

    with pytest.raises(ToolError):
        _toolset(tmp_path).create_mcp(
            {"good": STDIO, "bad": {"type": "stdio"}},
        )

    assert target.read_text(encoding="utf-8") == before


def test_broken_json_is_never_clobbered(tmp_path: Path) -> None:
    target = tmp_path / ".mcp.json"
    target.write_text("{oops", encoding="utf-8")

    with pytest.raises(ToolError) as excinfo:
        _toolset(tmp_path).create_mcp({"demo": STDIO})

    assert "未改动任何内容" in str(excinfo.value)
    assert target.read_text(encoding="utf-8") == "{oops"


def test_non_object_servers_section_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / ".mcp.json"
    target.write_text(json.dumps({"mcpServers": []}), encoding="utf-8")

    with pytest.raises(ToolError):
        _toolset(tmp_path).create_mcp({"demo": STDIO})


def test_empty_or_duplicated_names_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ToolError):
        _toolset(tmp_path).create_mcp({})
    with pytest.raises(ToolError):
        _toolset(tmp_path).create_mcp({"  ": STDIO})
    with pytest.raises(ToolError):
        _toolset(tmp_path).create_mcp({"demo": STDIO, "DEMO": STDIO})
    assert not (tmp_path / ".mcp.json").exists()


def test_invalid_json_argument_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ToolError):
        _toolset(tmp_path).create_mcp('{"demo": ')


# -- 路径与环境变量 ---------------------------------------------------------


def test_env_placeholders_are_not_expanded_on_write(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("DEMO_TOKEN", raising=False)

    _toolset(tmp_path).create_mcp({"remote": REMOTE})

    written = _read(tmp_path / ".mcp.json")["mcpServers"]["remote"]
    assert written["headers"]["Authorization"] == "Bearer ${DEMO_TOKEN}"


def test_secret_values_are_not_echoed(tmp_path: Path) -> None:
    servers = {
        "remote": {
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer super-secret-token"},
        }
    }

    result = _toolset(tmp_path).create_mcp(servers)

    assert "super-secret-token" not in result
    assert "环境变量" in result


def test_explicit_config_path_becomes_default_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    shared = tmp_path / "shared.json"
    toolset = _toolset(project, config_path=str(shared))

    toolset.create_mcp({"demo": STDIO})

    assert set(_read(shared)["mcpServers"]) == {"demo"}
    assert not (project / ".mcp.json").exists()


def test_relative_path_parameter_is_based_on_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "configs").mkdir()
    project = tmp_path / "project"
    project.mkdir()

    _toolset(project, workspace_root=workspace).create_mcp(
        {"demo": STDIO},
        path="configs/custom.json",
    )

    assert set(_read(workspace / "configs" / "custom.json")["mcpServers"]) == {"demo"}
    assert not (project / ".mcp.json").exists()


def test_missing_parent_directory_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ToolError) as excinfo:
        _toolset(tmp_path).create_mcp({"demo": STDIO}, path="nope/custom.json")
    assert "目录不存在" in str(excinfo.value)


def test_non_json_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ToolError):
        _toolset(tmp_path).create_mcp({"demo": STDIO}, path="servers.txt")


def test_no_temporary_files_are_left(tmp_path: Path) -> None:
    _toolset(tmp_path).create_mcp({"demo": STDIO})
    assert [item.name for item in tmp_path.iterdir()] == [".mcp.json"]


# -- 与运行期解析器、注册表、组合根的一致性 ---------------------------------


def test_written_config_is_readable_by_runtime_loader(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DEMO_TOKEN", "live-token")
    _toolset(tmp_path).create_mcp({"demo": STDIO, "remote": REMOTE})

    config = load_mcp_config(tmp_path)

    assert config.issues == ()
    assert {server.name for server in config.servers} == {"demo", "remote"}
    remote = next(server for server in config.servers if server.name == "remote")
    assert dict(remote.headers) == {"Authorization": "Bearer live-token"}


def test_registry_exposes_create_mcp(tmp_path: Path) -> None:
    toolset = _toolset(tmp_path)
    registry = ToolRegistry(toolset.registry_handlers(), MCP_CONFIG_TOOL_SPECS)

    result = registry.execute(
        "createMcp", json.dumps({"servers": {"demo": STDIO}})
    )

    assert result.ok, result.content
    assert ".mcp.json" in result.content
    assert "demo" in _read(tmp_path / ".mcp.json")["mcpServers"]

    unknown = registry.execute("createMcp", json.dumps({"servers": "bad"}))
    assert not unknown.ok


def test_build_agent_registers_create_mcp(tmp_path: Path) -> None:
    config = AppConfig(
        workspace=tmp_path,
        llm=LLMSettings(api_key="test-key"),
        limits=AgentLimits(),
    )

    agent = build_agent(
        config,
        project_root=tmp_path,
        bundled_roots=(),
        mcp_settings=McpSettings(enabled=False),
    )

    assert any(spec.name == "createMcp" for spec in agent.tool_specs)
    assert "createMcp" in agent.system_prompt
