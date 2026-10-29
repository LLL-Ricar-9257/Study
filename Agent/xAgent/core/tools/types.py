"""工具描述与执行结果。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个可被模型调用的工具声明。"""

    name: str
    description: str
    parameters: dict[str, Any]

    def as_openai_schema(self) -> dict[str, Any]:
        """返回 OpenAI function-calling 格式（同时兼容多数国产模型）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    """工具执行结果。失败是正常业务分支，不向 UI 抛异常。"""

    content: str
    ok: bool = True
