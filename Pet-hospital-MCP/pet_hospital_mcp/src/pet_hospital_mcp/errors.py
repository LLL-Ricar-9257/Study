"""统一结构化错误定义。

所有对 MCP 客户端暴露的失败都必须是同一个信封::

    {
      "error": {
        "code": "ERROR_CODE",
        "message": "可读错误信息",
        "details": {}
      }
    }

本模块同时提供：

* :class:`ErrorCode` —— 错误码常量；
* :class:`ErrorInfo` / :class:`ToolErrorOutput` —— 错误输出的 Pydantic 模型；
* :class:`PetHospitalMCPError` 及其子类 —— 内部异常类型，序列化时产出上面的信封。

约定：``details`` 只放结构化、可安全展示的事实（HTTP 状态码、字段名、可选枚举），
绝不放 Python 堆栈、httpx 异常文本或完整的上游响应体。
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ErrorCode",
    "ErrorInfo",
    "ToolErrorOutput",
    "PetHospitalMCPError",
    "ValidationError",
    "BackendTimeoutError",
    "BackendUnavailableError",
    "BackendApiError",
    "BackendInvalidResponseError",
    "InternalError",
]


class ErrorCode:
    """错误码常量集合（教学场景，保持简单，不做枚举继承）。"""

    VALIDATION_ERROR: Final = "VALIDATION_ERROR"
    BACKEND_TIMEOUT: Final = "BACKEND_TIMEOUT"
    BACKEND_UNAVAILABLE: Final = "BACKEND_UNAVAILABLE"
    BACKEND_API_ERROR: Final = "BACKEND_API_ERROR"
    BACKEND_INVALID_RESPONSE: Final = "BACKEND_INVALID_RESPONSE"
    INTERNAL_ERROR: Final = "INTERNAL_ERROR"


ErrorCodeLiteral = Literal[
    "VALIDATION_ERROR",
    "BACKEND_TIMEOUT",
    "BACKEND_UNAVAILABLE",
    "BACKEND_API_ERROR",
    "BACKEND_INVALID_RESPONSE",
    "INTERNAL_ERROR",
]

MAX_DETAIL_ITEMS: Final = 20
"""``details`` 中列表型字段最多保留的条目数，避免把上游整包数据塞进错误信息。"""


class ErrorInfo(BaseModel):
    """错误信封的 ``error`` 部分。"""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCodeLiteral = Field(description="机器可读错误码")
    message: str = Field(description="面向调用方的可读错误信息")
    details: dict[str, Any] = Field(default_factory=dict, description="结构化上下文，不含堆栈与完整响应体")


class ToolErrorOutput(BaseModel):
    """工具失败时返回给客户端的统一结构。"""

    model_config = ConfigDict(extra="forbid")

    error: ErrorInfo


class PetHospitalMCPError(Exception):
    """本服务所有可预期异常的基类。"""

    code: str = ErrorCode.INTERNAL_ERROR
    default_message: str = "服务内部错误"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.details = _cap_details(details or {})
        super().__init__(self.message)

    def to_output(self) -> ToolErrorOutput:
        """转成错误输出模型。"""
        return ToolErrorOutput(
            error=ErrorInfo(code=self.code, message=self.message, details=self.details)  # type: ignore[arg-type]
        )

    def to_json(self) -> str:
        """转成可直接放进工具结果文本的紧凑 JSON。"""
        return self.to_output().model_dump_json()


class ValidationError(PetHospitalMCPError):
    """工具入参不满足 Pydantic 输入模型。"""

    code = ErrorCode.VALIDATION_ERROR
    default_message = "工具入参校验失败"


class BackendTimeoutError(PetHospitalMCPError):
    """调用 Go REST API 超时。"""

    code = ErrorCode.BACKEND_TIMEOUT
    default_message = "调用宠物医院 REST API 超时"


class BackendUnavailableError(PetHospitalMCPError):
    """Go REST API 无法连接（服务未启动、连接被拒绝等）。"""

    code = ErrorCode.BACKEND_UNAVAILABLE
    default_message = "宠物医院 REST API 不可用，请确认 Go 服务已启动"


class BackendApiError(PetHospitalMCPError):
    """Go REST API 正常响应，但返回了 4xx / 5xx 或非 200 的业务码。"""

    code = ErrorCode.BACKEND_API_ERROR
    default_message = "宠物医院 REST API 返回错误"


class BackendInvalidResponseError(PetHospitalMCPError):
    """Go REST API 返回了无法解析或不符合数据契约的内容。"""

    code = ErrorCode.BACKEND_INVALID_RESPONSE
    default_message = "宠物医院 REST API 返回的数据不符合约定"


class InternalError(PetHospitalMCPError):
    """兜底：任何未被归类的异常。"""

    code = ErrorCode.INTERNAL_ERROR
    default_message = "服务内部错误"


def _cap_details(details: dict[str, Any]) -> dict[str, Any]:
    """截断过长的 ``details``，避免错误信息本身变成数据泄露面。"""
    capped: dict[str, Any] = {}
    for key, value in details.items():
        if isinstance(value, list):
            capped[key] = value[:MAX_DETAIL_ITEMS]
        elif isinstance(value, str) and len(value) > 512:
            capped[key] = value[:512] + "…"
        else:
            capped[key] = value
    return capped