"""结构化日志：JSONL 落盘 + 按大小轮转。

设计取舍：

* **只写文件，不写终端**。TUI 独占终端，任何 console handler 都会把界面打花。
* 一行一个 JSON 对象，字段固定：``ts`` / ``level`` / ``logger`` / ``msg``，
  其余通过 ``extra=`` 传入的字段原样附加，便于用 ``jq`` 过滤。
* 敏感字段（``api_key`` / ``authorization`` / ``token`` 等）在序列化前递归脱敏。
* 目录或文件不可写时**降级为无日志**，不打断 Agent 主流程。

用法::

    from core.logging import configure_logging
    path = configure_logging(config.logs, project_root=Path.cwd())
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Final

from .config import LogSettings

__all__ = [
    "REDACTED",
    "JsonLineFormatter",
    "configure_logging",
    "current_log_path",
    "reset_logging",
]

REDACTED: Final = "***"
_SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "client_secret",
    }
)
_RESERVED: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_LOG_PATH: Path | None = None
_CONFIGURED = False


def redact(value: Any) -> Any:
    """递归替换敏感字段的值。"""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = key if isinstance(key, str) else str(key)
            if key_text.strip().casefold().replace("-", "_") in _SENSITIVE_KEYS:
                result[key_text] = REDACTED
            else:
                result[key_text] = redact(item)
        return result
    if isinstance(value, (list, tuple)):
        rendered = [redact(item) for item in value]
        return type(value)(rendered) if isinstance(value, tuple) else rendered
    return value


class JsonLineFormatter(logging.Formatter):
    """把一条日志渲染成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = redact(value)
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc"] = record.exc_info[0].__name__
        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover - 兜底
            return json.dumps(
                {"ts": payload["ts"], "level": payload["level"], "msg": payload["msg"]},
                ensure_ascii=False,
            )


def configure_logging(
    settings: LogSettings | None,
    *,
    project_root: str | Path,
    logger_name: str = "",
) -> Path | None:
    """按配置挂上文件 handler，返回日志文件路径；未启用或失败时返回 ``None``。

    重复调用是幂等的：已经配置过就不再重复添加 handler。
    """
    global _LOG_PATH, _CONFIGURED

    if settings is None or not settings.enabled:
        return None
    if _CONFIGURED:
        return _LOG_PATH

    root = Path(project_root).expanduser().resolve()
    directory = Path(settings.directory).expanduser()
    if not directory.is_absolute():
        directory = root / directory

    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"xagent-{datetime.now().strftime('%Y%m%d')}.jsonl"
        handler = RotatingFileHandler(
            path,
            maxBytes=settings.max_bytes,
            backupCount=settings.backup_count,
            encoding="utf-8",
            delay=True,
        )
    except OSError:
        # 目录不可写：降级为无文件日志，绝不因此影响 Agent 启动。
        logging.getLogger(__name__).warning("log_file_unavailable", extra={"dir": str(directory)})
        return None

    handler.setFormatter(JsonLineFormatter())
    handler.setLevel(getattr(logging, settings.level, logging.INFO))
    handler._xagent_owned = True  # type: ignore[attr-defined]  # 便于 reset_logging 精确定位

    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > handler.level:
        logger.setLevel(handler.level)
    # 第三方库的噪声压到 WARNING，只保留本项目的调用日志。
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _LOG_PATH = path
    _CONFIGURED = True
    logger.info(
        "logging_started",
        extra={"path": str(path), "level": settings.level},
    )
    return path


def current_log_path() -> Path | None:
    """当前进程使用的日志文件；未配置时为 ``None``（供 ``/session`` 等展示）。"""
    return _LOG_PATH


def reset_logging(logger_name: str = "") -> None:
    """移除 ``configure_logging`` 添加的 handler 并复位内部状态（测试用）。"""
    global _LOG_PATH, _CONFIGURED

    logger = logging.getLogger(logger_name)
    for handler in list(logger.handlers):
        if getattr(handler, "_xagent_owned", False):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover - 关闭失败不影响复位
                pass
    _LOG_PATH = None
    _CONFIGURED = False
