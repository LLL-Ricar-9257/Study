from __future__ import annotations

from pathlib import Path

import pytest

from core.config import _discover_config_path, load_config
from core.errors import ConfigurationError


def _write_config(path: Path, *, workspace: str = "", api_key: str = "test-key") -> Path:
    path.write_text(
        "[llm]\n"
        f"api_key = {api_key}\n"
        "base_url = https://example.invalid/v1\n"
        "model = test-model\n"
        "timeout_seconds = 30\n"
        "\n[agent]\n"
        f"workspace = {workspace}\n"
        "max_tool_rounds = 3\n"
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
