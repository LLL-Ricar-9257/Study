from __future__ import annotations

from pathlib import Path

import pytest

from core.config import (
    CONFIG_TEMPLATE,
    AgentLimits,
    ConfigFileCreatedError,
    _discover_config_path,
    load_config,
    write_default_config,
)
from core.errors import ConfigurationError


def _write_config(
    path: Path,
    *,
    workspace: str = "",
    api_key: str = "test-key",
    max_tool_rounds: str = "3",
) -> Path:
    path.write_text(
        "[llm]\n"
        f"api_key = {api_key}\n"
        "base_url = https://example.invalid/v1\n"
        "model = test-model\n"
        "timeout_seconds = 30\n"
        "\n[agent]\n"
        f"workspace = {workspace}\n"
        f"max_tool_rounds = {max_tool_rounds}\n"
        "max_text_bytes = 1024\n"
        "default_max_chars = 100\n"
        "max_list_items = 10\n",
        encoding="utf-8",
    )
    return path


def test_load_config_and_relative_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("XAGENT_CONFIG", "XAGENT_WORKSPACE", "XAGENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_file = _write_config(tmp_path / "config.ini", workspace="ws")

    config = load_config(config_file)
    assert config.workspace == workspace.resolve()
    assert config.llm.api_key == "test-key"
    assert config.llm.model == "test-model"
    assert config.llm.timeout_seconds == 30
    assert config.limits.max_tool_rounds == 3
    assert config.limits.max_list_items == 10
    assert config.config_path == config_file.resolve()


def test_explicit_workspace_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XAGENT_API_KEY", raising=False)
    config_file = _write_config(tmp_path / "config.ini", workspace="ignored")
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    config = load_config(config_file, workspace=explicit)
    assert config.workspace == explicit.resolve()


def test_api_key_is_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAGENT_API_KEY", raising=False)
    config_file = _write_config(tmp_path / "config.ini", workspace=".")
    config = load_config(config_file)
    assert "test-key" not in repr(config)
    assert "test-key" not in repr(config.llm)


def test_missing_api_key_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("XAGENT_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    config_file = _write_config(tmp_path / "config.ini", workspace=".", api_key="")
    with pytest.raises(ConfigurationError, match="缺少 API Key"):
        load_config(config_file)


def test_missing_explicit_config_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="找不到配置文件"):
        _discover_config_path(tmp_path / "nope.ini")


@pytest.mark.parametrize("value", ["", "0", "none", "unlimited"])
def test_max_tool_rounds_can_be_unlimited(
    tmp_path: Path, value: str
) -> None:
    config_file = _write_config(tmp_path / "config.ini", max_tool_rounds=value)
    config = load_config(config_file)
    assert config.limits.max_tool_rounds is None


@pytest.mark.parametrize("value", ["-1", "abc"])
def test_invalid_max_tool_rounds_is_rejected(
    tmp_path: Path, value: str
) -> None:
    config_file = _write_config(tmp_path / "config.ini", max_tool_rounds=value)
    with pytest.raises(ConfigurationError, match="max_tool_rounds"):
        load_config(config_file)


def test_agent_limits_zero_tool_rounds_means_unlimited() -> None:
    assert AgentLimits(max_tool_rounds=0).max_tool_rounds is None
    with pytest.raises(ConfigurationError, match="max_tool_rounds"):
        AgentLimits(max_tool_rounds=-1)


def _write_search_config(path: Path, *, search: str) -> Path:
    path.write_text(
        "[llm]\n"
        "api_key = test-key\n"
        "\n[agent]\n"
        "workspace = .\n"
        "\n[search]\n" + search,
        encoding="utf-8",
    )
    return path


def test_load_search_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("XAGENT_BAIDU_QIANFAN_API_KEY", "BAIDU_QIANFAN_API_KEY", "XAGENT_SEARCH_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    config_file = _write_search_config(
        tmp_path / "config.ini",
        search=(
            "baidu_qianfan_api_key = baidu-key\n"
            "api_url = https://example.invalid/search\n"
            "top_k = 5\n"
            "timeout_seconds = 8\n"
        ),
    )

    config = load_config(config_file)
    assert config.search.api_key == "baidu-key"
    assert config.search.endpoint == "https://example.invalid/search"
    assert config.search.top_k == 5
    assert config.search.timeout_seconds == 8
    assert "baidu-key" not in repr(config.search)
    assert "baidu-key" not in repr(config)


def test_search_env_overrides_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XAGENT_BAIDU_QIANFAN_API_KEY", "env-key")
    config_file = _write_search_config(
        tmp_path / "config.ini", search="baidu_qianfan_api_key = file-key\n"
    )
    config = load_config(config_file)
    assert config.search.api_key == "env-key"


def test_missing_search_key_does_not_block_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XAGENT_BAIDU_QIANFAN_API_KEY", raising=False)
    monkeypatch.delenv("BAIDU_QIANFAN_API_KEY", raising=False)
    config_file = _write_config(tmp_path / "config.ini", workspace=".")
    config = load_config(config_file)
    assert config.search.api_key == ""
    assert config.search.top_k == 10


def test_invalid_search_top_k_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XAGENT_BAIDU_QIANFAN_API_KEY", raising=False)
    config_file = _write_search_config(
        tmp_path / "config.ini", search="top_k = 100\n"
    )
    with pytest.raises(ConfigurationError, match="top_k"):
        load_config(config_file)


def test_template_contains_all_sections() -> None:
    for section in (
        "[llm]",
        "[agent]",
        "[context]",
        "[sessions]",
        "[tui]",
        "[tools]",
        "[search]",
        "[mcp]",
    ):
        assert section in CONFIG_TEMPLATE


def test_missing_config_is_created_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "core.config.DEFAULT_CONFIG_PATH",
        tmp_path / "does-not-exist.ini",
    )
    monkeypatch.delenv("XAGENT_CONFIG", raising=False)
    with pytest.raises(ConfigFileCreatedError) as excinfo:
        load_config()
    created = excinfo.value.path
    assert created == tmp_path / "config.ini"
    assert created.is_file()
    assert "api_key = sk-mock-replace-me" in created.read_text(encoding="utf-8")


def test_write_default_config_does_not_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "config.ini"
    path.write_text("[llm]\napi_key = keep-me\n", encoding="utf-8")
    returned = write_default_config(path)
    assert returned == path
    assert "keep-me" in path.read_text(encoding="utf-8")


def test_load_new_sections_and_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("XAGENT_CONFIG", "XAGENT_API_KEY", "XAGENT_MCP_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.ini"
    path.write_text(
        "[llm]\n"
        "api_key = test-key\n"
        "[agent]\nworkspace = .\n"
        "[context]\n"
        "default_window = 64000\n"
        "warning_ratio = 0.75\n"
        "summary_tokens = 3000\n"
        "archive_dir = .custom/context\n"
        "[sessions]\n"
        "directory = .custom/sessions\n"
        "save_on_exit = false\n"
        "agents_file = PROJECT.md\n"
        "[tui]\nmcp_max_tools = 5\n"
        "[tools]\nruncli_timeout_seconds = 12\n"
        "[mcp]\nenabled = false\nconnect_timeout = 7\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.context.default_window == 64_000
    assert config.context.warning_ratio == 0.75
    assert config.context.summary_tokens == 3_000
    assert config.context.archive_dir == ".custom/context"
    assert config.sessions.directory == ".custom/sessions"
    assert config.sessions.save_on_exit is False
    assert config.sessions.agents_file == "PROJECT.md"
    assert config.tui.mcp_max_tools == 5
    assert config.tools.runcli_timeout_seconds == 12
    assert config.mcp.enabled is False
    assert config.mcp.to_settings().connect_timeout == 7


def test_invalid_warning_ratio_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XAGENT_API_KEY", raising=False)
    path = tmp_path / "config.ini"
    path.write_text(
        "[llm]\napi_key = test-key\n[context]\nwarning_ratio = 1.5\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="warning_ratio"):
        load_config(path)


def test_cli_shows_dialog_after_creating_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tui import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("XAGENT_CONFIG", raising=False)
    monkeypatch.setattr(
        "core.config.DEFAULT_CONFIG_PATH",
        tmp_path / "does-not-exist.ini",
    )
    calls: list[tuple[str, str]] = []

    def fake_dialog(title: str, body: str, *, button: str = "退出") -> bool:
        calls.append((title, body))
        return True

    monkeypatch.setattr("tui.dialog.show_notice_dialog", fake_dialog)
    assert cli.main([]) == 2
    assert (tmp_path / "config.ini").is_file()
    assert calls
    assert "config.ini" in calls[0][1]
    assert "重新启动" in calls[0][1]

