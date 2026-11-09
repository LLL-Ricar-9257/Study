"""统一错误信封。

所有对 MCP 客户端暴露的失败都是同一个结构::

    {"error": {"code": "API_ERROR", "message": "可读信息", "details": {...}}}

约定：``details`` 只放可安全展示的结构化事实（HTTP 状态、服务器返回的错误码、
字段名），绝不放堆栈、访问令牌或完整响应体。
"""

from __future__ import annotations

import json
from typing import Any, Final

__all__ = [
    "MAX_DETAIL_CHARS",
    "AnythingLLMError",
    "ApiError",
    "BackendTimeoutError",
    "BackendUnavailableError",
    "ConfigurationError",
    "ErrorCode",
    "InternalError",
    "InvalidResponseError",
    "ValidationError",
]

MAX_DETAIL_CHARS: Final = 512
"""``details`` 里单个字符串的最大长度，避免错误信息本身变成数据泄露面。"""


class ErrorCode:
    """机器可读错误码。"""

    CONFIG_ERROR: Final = "CONFIG_ERROR"
    VALIDATION_ERROR: Final = "VALIDATION_ERROR"
    BACKEND_TIMEOUT: Final = "BACKEND_TIMEOUT"
    BACKEND_UNAVAILABLE: Final = "BACKEND_UNAVAILABLE"
    API_ERROR: Final = "API_ERROR"
    INVALID_RESPONSE: Final = "INVALID_RESPONSE"
    INTERNAL_ERROR: Final = "INTERNAL_ERROR"


class AnythingLLMError(Exception):
    """本服务所有可预期异常的基类。"""

    code: str = ErrorCode.INTERNAL_ERROR
    default_message: str = "AnythingLLM 调用失败"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.details = _cap(details or {})
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class ConfigurationError(AnythingLLMError):
    """缺少 API Key 或服务地址不可用。"""

    code = ErrorCode.CONFIG_ERROR
    default_message = "AnythingLLM 配置不完整"


class ValidationError(AnythingLLMError):
    """工具入参不合法（此时不会发出任何 HTTP 请求）。"""

    code = ErrorCode.VALIDATION_ERROR
    default_message = "工具入参校验失败"


class BackendTimeoutError(AnythingLLMError):
    """请求超时（已按配置重试仍失败）。"""

    code = ErrorCode.BACKEND_TIMEOUT
    default_message = "调用 AnythingLLM 超时"


class BackendUnavailableError(AnythingLLMError):
    """连不上 AnythingLLM（服务未启动、地址写错、被防火墙拦截）。"""

    code = ErrorCode.BACKEND_UNAVAILABLE
    default_message = "无法连接 AnythingLLM，请确认服务已启动且地址正确"


class ApiError(AnythingLLMError):
    """HTTP 4xx/5xx，或 AnythingLLM 返回 ``success: false`` / ``error``。"""

    code = ErrorCode.API_ERROR
    default_message = "AnythingLLM 返回错误"


class InvalidResponseError(AnythingLLMError):
    """响应不是合法 JSON，或缺少数必需字段。"""

    code = ErrorCode.INVALID_RESPONSE
    default_message = "AnythingLLM 返回的数据不符合约定"


class InternalError(AnythingLLMError):
    """兜底：任何未被归类的异常。"""

    code = ErrorCode.INTERNAL_ERROR
    default_message = "服务内部错误"


def _cap(details: dict[str, Any]) -> dict[str, Any]:
    """截断过长的字段值。"""
    capped: dict[str, Any] = {}
    for key, value in details.items():
        if isinstance(value, str) and len(value) > MAX_DETAIL_CHARS:
            capped[key] = value[:MAX_DETAIL_CHARS] + "…"
        elif isinstance(value, list):
            capped[key] = value[:20]
        else:
            capped[key] = value
    return capped
