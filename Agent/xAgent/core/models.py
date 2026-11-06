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
class TokenUsage:
    """一次模型请求的 token 用量。

    ``cached_tokens`` / ``cache_miss_tokens`` 使用 ``None`` 表示供应商
    没有返回 Prompt 缓存信息，避免把“未知”误报成 0。不同供应商字段名不同：
    DeepSeek 使用 ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``，
    OpenAI 兼容接口通常使用 ``prompt_tokens_details.cached_tokens``。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int | None = None
    cache_miss_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ChatChunk:
    """模型流式输出的统一分片。"""

    text: str = ""
    tool_call_deltas: tuple[ToolCallDelta, ...] = field(default_factory=tuple)
    usage: TokenUsage | None = None
    """部分供应商只在流的最后一个分片返回用量；不需要用量的调用方可以忽略。"""
