"""结构化日志与递归脱敏。

设计要点：

* 使用标准库 ``logging`` + JSON Formatter，不引入 structlog；
* 每次工具调用固定输出一条记录，包含 ``timestamp`` / ``tool_name`` /
  ``params`` / ``status`` / ``duration_ms``；
* :func:`redact` 会递归遍历任意结构，把 ``ownerPhone`` / ``ownerAddr`` /
  ``chipNo`` 及其 snake_case 写法替换成掩码，且不依赖调用方自觉。
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "REDACTED",
    "SENSITIVE_KEYS",
    "ToolCallRecord",
    "configure_logging",
    "get_logger",
    "redact",
    "tool_call_scope",
]

REDACTED: Final = "***"

SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "ownerphone",
        "owneraddr",
        "chipno",
    }
)
"""归一化（小写、去掉下划线）后需要脱敏的字段名。"""

_RESERVED: Final = frozenset(
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


def normalize_key(key: str) -> str:
    """把字段名归一化，用于大小写 / 命名风格无关的比对。"""
    return key.replace("_", "").replace("-", "").lower()


def redact(value: Any) -> Any:
    """递归脱敏。命中敏感字段名的值替换为 :data:`REDACTED`，其余结构原样返回。"""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_str = key if isinstance(key, str) else str(key)
            if normalize_key(key_str) in SENSITIVE_KEYS:
                result[key_str] = REDACTED
            else:
                result[key_str] = redact(item)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        rendered = [redact(item) for item in value]
        return type(value)(rendered) if isinstance(value, tuple) else rendered
    return value


class JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = redact(value)
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "Unknown"
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", *, stream: Any = None) -> None:
    """配置根 logger：单一 JSON handler，避免重复输出。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # httpx 每个请求都打 INFO 日志，会淹没工具调用记录。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """取得 logger。"""
    return logging.getLogger(name)


@dataclass(slots=True)
class ToolCallRecord:
    """一次工具调用的日志累加器。"""

    tool_name: str
    params: dict[str, Any] = field(default_factory=dict)
    status: str = "success"
    error_code: str | None = None

    def fail(self, error_code: str) -> None:
        self.status = "error"
        self.error_code = error_code


@contextmanager
def tool_call_scope(
    logger: logging.Logger, tool_name: str, params: Mapping[str, Any]
) -> Iterator[ToolCallRecord]:
    """包住一次工具调用：无论成功失敗都输出一条含耗时的结构化日志。

    用法::

        with tool_call_scope(logger, "list_pets", params) as record:
            ...
            record.fail(err.code)
    """
    record = ToolCallRecord(tool_name=tool_name, params=dict(params))
    started = time.perf_counter()
    try:
        yield record
    except BaseException:
        record.status = "crash"
        raise
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        logger.info(
            "tool_call",
            extra={
                "tool_name": record.tool_name,
                "params": redact(record.params),
                "status": record.status,
                "duration_ms": duration_ms,
                "error_code": record.error_code,
            },
        )


def summarize_for_log(params: Mapping[str, Any], *, max_items: int = 10) -> dict[str, Any]:
    """把参数整理成适合写日志的形状：脱敏 + 列表截断 + 去空。"""
    cleaned: dict[str, Any] = {}
    for key, value in redact(dict(params)).items():
        if value is None:
            continue
        if isinstance(value, Sequence) and not isinstance(value, str):
            value = list(value)
            if len(value) > max_items:
                value = value[:max_items] + [f"…(+{len(value) - max_items})"]
        cleaned[key] = value
    return cleaned