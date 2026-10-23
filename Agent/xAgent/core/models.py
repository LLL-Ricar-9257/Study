"""与具体模型供应商无关的对话数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型请求的一次工具调用。"""

    id: str
    name: str
    arguments: str
    """原始 JSON 字符串。是否合法由工具注册表负责校验。"""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一条对话消息。

    这是核心层自己的消息格式。LLM 适配器负责把它转换成
    OpenAI、Anthropic 或其它供应商需要的结构。
    """

    role: MessageRole
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    @classmethod
    def system(cls, content: str) -> ChatMessage:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> ChatMessage:
        return cls(role="user", content=content)

    @classmethod
    def assistant(
        cls,
        content: str | None = None,
        tool_calls: tuple[ToolCall, ...] = (),
    ) -> ChatMessage:
        return cls(role="assistant", content=content, tool_calls=tool_calls)

    @classmethod
    def tool(cls, tool_call_id: str, content: str) -> ChatMessage:
        return cls(role="tool", content=content, tool_call_id=tool_call_id)


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """流式返回的工具调用增量。"""

    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class ChatChunk:
    """模型流式输出的统一分片。"""

    text: str = ""
    tool_call_deltas: tuple[ToolCallDelta, ...] = field(default_factory=tuple)
