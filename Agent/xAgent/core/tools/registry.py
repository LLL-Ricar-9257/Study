"""工具注册与安全执行。

注册表是 Agent 与具体文件工具之间的一层边界：
- 校验工具名和 JSON 参数；
- 把任意异常转换为可回传给模型的 ToolResult；
- 向 LLM 输出供应商无关的工具声明。
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .specs import FILE_TOOL_SPECS
from .types import ToolResult, ToolSpec

ToolHandler = Callable[..., str]


class ToolRegistry:
    def __init__(
        self,
        handlers: Mapping[str, ToolHandler],
        specs: Sequence[ToolSpec] = FILE_TOOL_SPECS,
    ) -> None:
        self._handlers = dict(handlers)
        self._specs = {spec.name: spec for spec in specs}

        missing = sorted(set(self._specs) - set(self._handlers))
        extra = sorted(set(self._handlers) - set(self._specs))
        if missing:
            raise ValueError(f"工具缺少实现：{', '.join(missing)}")
        if extra:
            raise ValueError(f"工具缺少声明：{', '.join(extra)}")

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def register(
        self,
        handlers: Mapping[str, ToolHandler],
        specs: Sequence[ToolSpec],
    ) -> tuple[str, ...]:
        """动态注册工具（用于 MCP 后台加载），返回被跳过的重名工具。

        与本地工具重名的 MCP 工具不会覆盖本地实现；重复注册同一个工具
        视为幂等，避免重连后出现两份声明。
        """

        skipped: list[str] = []
        for spec in specs:
            handler = handlers.get(spec.name)
            if handler is None or spec.name in self._specs:
                skipped.append(spec.name)
                continue
            self._handlers[spec.name] = handler
            self._specs[spec.name] = spec
        return tuple(skipped)

    def schema(self) -> list[dict[str, Any]]:
        """给 LLM 的工具声明。"""
        return [spec.as_openai_schema() for spec in self._specs.values()]

    def execute(self, name: str, raw_arguments: str) -> ToolResult:
        """执行工具，永远返回 ToolResult，不把异常抛给 Agent 循环。"""
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(f"未知工具：{name}", ok=False)

        try:
            arguments = json.loads(raw_arguments or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            return ToolResult(f"工具 {name} 参数不是合法 JSON：{exc}", ok=False)
        if not isinstance(arguments, dict):
            return ToolResult(f"工具 {name} 的参数必须是 JSON 对象", ok=False)

        try:
            inspect.signature(handler).bind(**arguments)
        except TypeError as exc:
            return ToolResult(f"工具 {name} 参数不匹配：{exc}", ok=False)

        try:
            output = handler(**arguments)
        except Exception as exc:  # 工具错误必须回传给模型
            return ToolResult(f"工具 {name} 执行失败：{exc}", ok=False)
        return ToolResult(str(output), ok=True)
