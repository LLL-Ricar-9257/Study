"""日志落盘：JSONL 格式、脱敏、只写文件、失败降级。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from core.config import ConfigurationError, LogSettings
from core.logging import (
    REDACTED,
    configure_logging,
    current_log_path,
    redact,
    reset_logging,
)


@pytest.fixture(autouse=True)
def _clean_logging():
    reset_logging()
    yield
    reset_logging()


def _read_lines(path: Path) -> list[dict]:
    handler_flush()
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def handler_flush() -> None:
    for handlers in logging.Logger.manager.loggerDict.values():
        if isinstance(handlers, logging.Logger):
            for handler in handlers.handlers:
                handler.flush()
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_writes_one_json_line_per_record(tmp_path: Path) -> None:
    settings = LogSettings(enabled=True, level="INFO", directory="logs")
    path = configure_logging(settings, project_root=tmp_path)

    assert path is not None and path.parent == tmp_path / "logs"
    assert current_log_path() == path

    logging.getLogger("core.agent").info(
        "tool_call", extra={"tool": "read_text_file", "ok": True, "elapsed_ms": 3}
    )

    lines = _read_lines(path)
    record = lines[-1]
    assert record["msg"] == "tool_call"
    assert record["tool"] == "read_text_file"
    assert record["ok"] is True
    assert record["level"] == "INFO"
    assert "ts" in record


def test_respects_level(tmp_path: Path) -> None:
    path = configure_logging(
        LogSettings(level="WARNING", directory="logs"), project_root=tmp_path
    )
    logging.getLogger("core.agent").info("should_be_dropped")
    logging.getLogger("core.agent").warning("kept")

    records = _read_lines(path)
    assert all(record["msg"] != "should_be_dropped" for record in records)
    assert any(record["msg"] == "kept" for record in records)


def test_disabled_returns_none(tmp_path: Path) -> None:
    assert configure_logging(LogSettings(enabled=False), project_root=tmp_path) is None
    assert current_log_path() is None


def test_unwritable_directory_degrades(tmp_path: Path, monkeypatch) -> None:
    """目录不可写时必须返回 None，而不是把异常抛给启动流程。"""
    original = Path.mkdir

    def fail_mkdir(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise OSError("denied")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    try:
        assert configure_logging(LogSettings(directory="logs"), project_root=tmp_path) is None
    finally:
        monkeypatch.setattr(Path, "mkdir", original)


def test_redact_masks_nested_secrets() -> None:
    payload = {
        "api_key": "sk-123",
        "nested": {"token": "t", "Authorization": "Bearer x", "safe": 1},
        "items": [{"password": "p"}],
    }
    masked = redact(payload)
    assert masked["api_key"] == REDACTED
    assert masked["nested"]["token"] == REDACTED
    assert masked["nested"]["Authorization"] == REDACTED
    assert masked["nested"]["safe"] == 1
    assert masked["items"][0]["password"] == REDACTED


def test_log_settings_validation() -> None:
    with pytest.raises(ConfigurationError):
        LogSettings(level="TRACE")
    with pytest.raises(ConfigurationError):
        LogSettings(max_bytes=10)
    with pytest.raises(ConfigurationError):
        LogSettings(backup_count=-1)
    with pytest.raises(ConfigurationError):
        LogSettings(directory="  ")


def test_rotating_handler_keeps_file_small(tmp_path: Path) -> None:
    path = configure_logging(
        LogSettings(directory="logs", max_bytes=1024, backup_count=1),
        project_root=tmp_path,
    )
    logger = logging.getLogger("core.agent")
    for index in range(200):
        logger.info("tool_call", extra={"tool": "x", "index": index})
    handler_flush()

    backups = list(path.parent.glob(path.name + ".*"))
    assert backups, "超过 max_bytes 应产生轮转备份"
