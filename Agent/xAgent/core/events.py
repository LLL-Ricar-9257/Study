"""核心层向任意 UI 输出的领域事件。

事件是 core 与 tui / web / desktop 之间唯一的“显示协议”。
UI 只消费这些不可变对象，不接触 OpenAI、文件系统或 Agent 内部状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True, slots=True)
class TurnStarted:
    """一轮用户请求开始（历史记录已接收用户输入）。"""

    user_text: str


@dataclass(frozen=True, slots=True)
class TextDelta:
    """助手正文的流式增量。"""

    text: str


@dataclass(frozen=True, slots=True)
class ToolStarted:
    """模型请求调用工具。arguments 是原始 JSON 字符串。"""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ToolFinished:
    """工具执行结束。失败也通过该事件返回，由 UI 决定样式。"""

    call_id: str
    name: str
    ok: bool
    content: str
    elapsed_ms: int = 0


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """一轮请求正常结束。"""

    content: str
    limit_reached: bool = False


@dataclass(frozen=True, slots=True)
class TurnFailed:
    """一轮请求失败，核心已回滚该轮对话。"""

    message: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ToolRoundLimitReached:
    """工具循环达到上限，核心将停止继续调用模型。"""

    limit: int
    message: str


AgentEvent: TypeAlias = (
    TurnStarted
    | TextDelta
    | ToolStarted
    | ToolFinished
    | TurnCompleted
    | TurnFailed
    | ToolRoundLimitReached
)

__all__ = [
    "AgentEvent",
    "TextDelta",
    "ToolFinished",
    "ToolRoundLimitReached",
    "ToolStarted",
    "TurnCompleted",
    "TurnFailed",
    "TurnStarted",
]
