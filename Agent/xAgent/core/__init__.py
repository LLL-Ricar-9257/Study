"""xAgent 核心层公共 API。

表现层只从这里导入业务能力，不深入 core 的内部模块。
"""

from __future__ import annotations

from .agent import FileAgent
from .bootstrap import build_agent
from .config import AgentLimits, AppConfig, LLMSettings, load_config
from .errors import (
    AgentBusyError,
    ConfigurationError,
    ModelCallError,
    ToolError,
    WorkspaceBoundaryError,
    WorkspaceError,
    XAgentError,
)
from .events import (
    AgentEvent,
    TextDelta,
    ToolFinished,
    ToolRoundLimitReached,
    ToolStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from .models import ChatChunk, ChatMessage, ToolCall, ToolCallDelta
from .tools import TOOL_COUNT
from .tools.registry import ToolRegistry
from .tools.types import ToolResult, ToolSpec

__version__ = "0.1.0"

__all__ = [
    "AgentBusyError",
    "AgentEvent",
    "AgentLimits",
    "AppConfig",
    "ChatChunk",
    "ChatMessage",
    "ConfigurationError",
    "FileAgent",
    "LLMSettings",
    "ModelCallError",
    "TOOL_COUNT",
    "TextDelta",
    "ToolCall",
    "ToolCallDelta",
    "ToolError",
    "ToolFinished",
    "ToolRegistry",
    "ToolResult",
    "ToolRoundLimitReached",
    "ToolSpec",
    "ToolStarted",
    "TurnCompleted",
    "TurnFailed",
    "TurnStarted",
    "WorkspaceBoundaryError",
    "WorkspaceError",
    "XAgentError",
    "__version__",
    "build_agent",
    "load_config",
]
