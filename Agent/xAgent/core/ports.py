"""核心层依赖的抽象端口（依赖倒置）。

Agent 只依赖 ChatModel 协议；OpenAI 只是其中一种实现。
未来接入本地模型、Anthropic 或录制回放时，不需要修改 Agent。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from .models import ChatChunk, ChatMessage


@runtime_checkable
class ChatModel(Protocol):
    """流式对话模型端口。"""

    def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatChunk]:
        """按顺序产生文本增量和工具调用增量。"""
        ...
