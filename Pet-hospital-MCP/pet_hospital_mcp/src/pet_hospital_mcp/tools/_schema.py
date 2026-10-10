"""工具入参的严格化处理。

背景：``mcp==2.0.0`` 根据函数签名合成 ``arg_model``，它既不拒绝未知字段，
也不在发布的 ``inputSchema`` 里带 ``additionalProperties: false``；而且 SDK 会把
Pydantic 的原始报错文本（含 ``errors.pydantic.dev`` 链接和调用方取值）直接放进
工具结果。本模块一次性把这两件事都收紧：

1. 把 ``arg_model`` 换成 ``extra="forbid"`` 的等价模型并刷新 ``inputSchema``；
2. 在该模型上挂一个 wrap 校验器——校验失败时不把 Pydantic 异常抛给 SDK，
   而是转成本服务统一的 ``{"error": {...}}`` 结构，经 :class:`ContextVar`
   交给工具函数抛出。这样客户端永远看不到 Pydantic / SDK 的原始文本。

后续阶段的新工具只要在 :func:`harden_tool_arguments` 里带上自己的工具名即可复用。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict, ValidationError as PydanticValidationError
from pydantic import create_model, model_validator

from ..errors import ValidationError

__all__ = ["harden_tool_arguments", "take_deferred_input_error", "reject_bool"]

_DEFERRED: ContextVar[str | None] = ContextVar("pet_hospital_mcp_deferred_error", default=None)


def reject_bool(value: Any) -> Any:
    """前置校验：``True`` / ``False`` 永远不是合法的数值入参。"""
    if isinstance(value, bool):
        raise ValueError("布尔值不是合法的数值参数")
    return value


def _describe(exc: PydanticValidationError) -> str:
    """把 Pydantic 错误压成统一结构，只留字段名与原因，不回显调用方取值。"""
    errors = [
        {
            "field": ".".join(str(part) for part in error["loc"]) or "(cross-field)",
            "reason": error["type"],
        }
        for error in exc.errors()[:20]
    ]
    return ValidationError("工具入参校验失败", details={"errors": errors}).to_json()


def _wrap_validator(cls: type[BaseModel], data: Any, handler: Any) -> Any:
    """校验失败时把统一错误暂存起来，并原样放行参数，交由工具体二次校验。

    ``mode="wrap"`` 的返回值就是最终结果，所以失败分支必须自己构造一个实例；
    用 :meth:`model_construct` 跳过二次校验，把原始取值原样带到工具体。
    """
    try:
        return handler(data)
    except PydanticValidationError as exc:
        _DEFERRED.set(_describe(exc))
        if isinstance(data, dict):
            return cls.model_construct(**{name: data.get(name) for name in cls.model_fields})
        raise  # pragma: no cover - 非 mapping 输入，交给 SDK 兜底


class _StrictArgumentsMixin:
    """带 wrap 校验器的入参模型基类。"""

    @model_validator(mode="wrap")  # type: ignore[arg-type]
    @classmethod
    def _pet_hospital_strict_arguments(cls, data: Any, handler: Any) -> Any:
        # 校验通过时也要清空暂存位，避免上一轮失败的结果泄漏到本轮。
        _DEFERRED.set(None)
        return _wrap_validator(cls, data, handler)


def harden_tool_arguments(server: MCPServer, tool_name: str) -> dict[str, Any]:
    """收紧 ``server`` 上名为 ``tool_name`` 的工具入参校验，返回收紧后的 ``inputSchema``。

    Raises:
        KeyError: 工具未注册。
    """
    tool = server._tool_manager.get_tool(tool_name)  # noqa: SLF001 - SDK 未提供公开的取用入口
    if tool is None:
        raise KeyError(f"tool {tool_name!r} is not registered")

    original = tool.fn_metadata.arg_model
    strict = create_model(
        original.__name__,
        __base__=(_StrictArgumentsMixin, original),
        __module__=original.__module__,
        __config__=ConfigDict(extra="forbid"),
    )
    strict.model_rebuild(force=True)

    tool.fn_metadata.arg_model = strict
    tool.parameters = strict.model_json_schema(by_alias=True)
    return tool.parameters


def take_deferred_input_error() -> str | None:
    """取出（并清空）暂存的统一入参错误 JSON；没有则返回 ``None``。"""
    deferred = _DEFERRED.get()
    _DEFERRED.set(None)
    return deferred