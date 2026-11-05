"""MCP 工具到本地 Function Calling 工具的适配。

两件事：

1. ``build_bindings``：把 MCP ``list_tools()`` 的结果转换为 xAgent 的
   ``ToolSpec``，并生成一个唯一的对外工具名。对外名称始终带服务器前缀
   （``<server>__<tool>``），从而避免不同 MCP Server 之间的工具名冲突，
   同时保留 对外名称 -> (服务器, 原始工具名) 的映射。
2. ``format_result``：把 MCP ``call_tool()`` 返回的文本块、图片/资源占位、
   结构化数据和错误标记整理成回传给 LLM 的可读文本。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..tools.types import ToolSpec

MAX_TOOL_NAME_LENGTH = 64
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_DROPPED_SCHEMA_KEYS = {"$schema", "$id"}


@dataclass(frozen=True, slots=True)
class McpToolBinding:
    """一个 MCP 工具与对外 Function Calling 工具名的绑定关系。"""

    exposed_name: str
    server_name: str
    tool_name: str
    spec: ToolSpec


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    """兼容 dict、pydantic 模型和不同 SDK 版本的字段命名。"""
    for name in names:
        if isinstance(obj, Mapping):
            if name in obj and obj[name] is not None:
                return obj[name]
            continue
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def sanitize_name_token(value: str, fallback: str) -> str:
    """把任意服务器/工具名转换为 OpenAI function name 允许的字符集。"""
    token = _NAME_UNSAFE.sub("_", str(value or "").strip()).strip("_-")
    return token or fallback


def _compose_name(server_token: str, tool_token: str) -> str:
    server_part = server_token[:40]
    composed = f"{server_part}__{tool_token}"
    if len(composed) > MAX_TOOL_NAME_LENGTH:
        composed = composed[:MAX_TOOL_NAME_LENGTH]
    return composed.rstrip("_-") or composed


def _unique_name(base: str, used: set[str]) -> str:
    candidate = base
    index = 2
    while candidate.casefold() in used:
        suffix = f"_{index}"
        head = base[: MAX_TOOL_NAME_LENGTH - len(suffix)].rstrip("_-")
        candidate = f"{head or base[:1]}{suffix}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _parameters_for(tool: Any) -> dict[str, Any] | None:
    """提取 MCP 工具的输入 Schema；无法用于 function calling 时返回 None。"""
    schema = _field(tool, "input_schema", "inputSchema", "parameters", default=None)
    if schema is None:
        return {"type": "object", "properties": {}}
    if not isinstance(schema, Mapping):
        return None

    parameters = {
        key: value
        for key, value in schema.items()
        if key not in _DROPPED_SCHEMA_KEYS
    }
    parameters.setdefault("type", "object")
    if parameters.get("type") != "object":
        return None

    properties = parameters.get("properties")
    if properties is None:
        parameters["properties"] = {}
    elif not isinstance(properties, Mapping):
        return None
    return parameters


def _description_for(server_name: str, tool_name: str, tool: Any) -> str:
    description = _field(tool, "description", default="") or ""
    description = " ".join(str(description).split())
    source = f"[来源] MCP 服务器「{server_name}」提供的工具 {tool_name}。"
    if description:
        return f"{description}\n\n{source}"
    return f"{source}该工具没有提供描述。"


def build_bindings(
    server_name: str,
    tools: Iterable[Any],
    *,
    reserved: Iterable[str] = (),
    used: set[str] | None = None,
) -> tuple[list[McpToolBinding], list[str]]:
    """把某台服务器的 MCP 工具转换为绑定关系。

    ``reserved`` 是本地内置工具名，``used`` 在多个服务器之间共享，
    保证注册到同一个 ``ToolRegistry`` 中的名称唯一。
    """
    taken = used if used is not None else set()
    taken.update(name.casefold() for name in reserved)

    server_token = sanitize_name_token(server_name, "mcp_server")
    bindings: list[McpToolBinding] = []
    issues: list[str] = []

    for tool in tools:
        tool_name = str(_field(tool, "name", default="") or "").strip()
        if not tool_name:
            issues.append("工具缺少 name，已跳过")
            continue

        parameters = _parameters_for(tool)
        if parameters is None:
            issues.append(
                f"工具 {tool_name} 的参数 Schema 不是 object，已跳过"
            )
            continue

        tool_token = sanitize_name_token(tool_name, "tool")
        exposed = _unique_name(
            _compose_name(server_token, tool_token),
            taken,
        )
        spec = ToolSpec(
            name=exposed,
            description=_description_for(server_name, tool_name, tool),
            parameters=parameters,
        )
        bindings.append(
            McpToolBinding(
                exposed_name=exposed,
                server_name=server_name,
                tool_name=tool_name,
                spec=spec,
            )
        )
    return bindings, issues


def _json_default(value: Any) -> Any:
    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return method(mode="json")
            except TypeError:
                try:
                    return method()
                except Exception:  # pragma: no cover - 极端兜底
                    break
            except Exception:  # pragma: no cover - 极端兜底
                break
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return str(value)


def to_json_text(value: Any, *, indent: int | None = 2) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            indent=indent,
            default=_json_default,
        )
    except (TypeError, ValueError):
        return str(value)


def _render_block(block: Any) -> str:
    kind = str(_field(block, "type", default="") or "").lower()

    text = _field(block, "text", default=None)
    if kind == "text" or (not kind and text is not None):
        return str(text or "")

    if kind == "image":
        mime = _field(block, "mime_type", "mimeType", default="未知类型")
        data = _field(block, "data", default="") or ""
        return f"[图片内容：{mime}，base64 长度 {len(str(data))}，未在文本中展开]"

    if kind == "audio":
        mime = _field(block, "mime_type", "mimeType", default="未知类型")
        data = _field(block, "data", default="") or ""
        return f"[音频内容：{mime}，base64 长度 {len(str(data))}，未在文本中展开]"

    if kind in {"resource_link", "resourceLink"}:
        uri = _field(block, "uri", default="")
        name = _field(block, "name", default="")
        label = f"{name} " if name else ""
        return f"[资源链接] {label}{uri}"

    resource = _field(block, "resource", default=None)
    if kind == "resource" or resource is not None:
        target = resource if resource is not None else block
        uri = _field(target, "uri", default="")
        embedded = _field(target, "text", default=None)
        if embedded is not None:
            return f"[嵌入资源 {uri}]\n{embedded}"
        blob = _field(target, "blob", default=None)
        if blob is not None:
            mime = _field(target, "mime_type", "mimeType", default="未知类型")
            return (
                f"[嵌入二进制资源 {uri}，{mime}，"
                f"base64 长度 {len(str(blob))}，未在文本中展开]"
            )
        return f"[嵌入资源 {uri}]"

    payload = to_json_text(block, indent=None)
    label = kind or "未知类型"
    return f"[{label} 内容] {payload}"


def _truncate(text: str, limit: int) -> tuple[str, str | None]:
    if limit > 0 and len(text) > limit:
        omitted = len(text) - limit
        return text[:limit], (
            f"结果过长已截断：仅保留前 {limit} 个字符，另有 {omitted} 个字符未显示。"
        )
    return text, None


def format_result(
    result: Any,
    *,
    server_name: str,
    tool_name: str,
    max_chars: int = 30_000,
) -> tuple[str, bool]:
    """把 ``CallToolResult`` 整理为文本，返回 ``(内容, 是否工具错误)``。"""
    blocks = _field(result, "content", default=[]) or []
    if isinstance(blocks, (str, bytes)) or not isinstance(blocks, Sequence):
        blocks = [blocks]

    sections: list[str] = []
    for block in blocks:
        rendered = _render_block(block)
        if rendered:
            sections.append(str(rendered))

    structured = _field(
        result,
        "structured_content",
        "structuredContent",
        default=None,
    )
    if structured is not None:
        sections.append("结构化数据（JSON）：\n" + to_json_text(structured))

    text = "\n\n".join(sections).strip()
    if not text:
        text = "（MCP 服务器返回了空结果。）"

    text, note = _truncate(text, max_chars)
    if note:
        text = f"{text}\n\n[截断提示] {note}"

    is_error = bool(_field(result, "is_error", "isError", default=False))
    if is_error:
        text = f"MCP 服务器「{server_name}」的工具 {tool_name} 返回错误：\n{text}"
    return text, is_error
