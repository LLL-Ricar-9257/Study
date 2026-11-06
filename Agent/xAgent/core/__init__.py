"""xAgent 核心层公共 API。

表现层只从这里导入业务能力，不深入 core 的内部模块。
"""

from __future__ import annotations

from .agent import CompactionResult, FileAgent, McpLoadResult
from .archive import (
    ArchivedContext,
    ContextArchive,
    RecallHit,
    RecallResult,
)
from .bootstrap import build_agent
from .config import (
    CONFIG_TEMPLATE,
    DEFAULT_CONFIG_PATH,
    AgentLimits,
    AppConfig,
    ConfigFileCreatedError,
    ContextSettings,
    LLMSettings,
    McpConfigSettings,
    SearchSettings,
    SessionSettings,
    ToolsSettings,
    TuiSettings,
    load_config,
    write_default_config,
)
from .context import (
    CONTEXT_WARNING_RATIO,
    DEFAULT_CONTEXT_WINDOW,
    ContextTracker,
    ContextUsage,
    estimate_messages_tokens,
    estimate_text_tokens,
)
from .errors import (
    AgentBusyError,
    ConfigurationError,
    ContextLimitError,
    ModelCallError,
    ToolError,
    WorkspaceBoundaryError,
    WorkspaceError,
    XAgentError,
)
from .events import (
    AgentEvent,
    ContextStatsUpdated,
    ContextWarning,
    TextDelta,
    ToolFinished,
    ToolRoundLimitReached,
    ToolStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from .mcp import (
    McpClientManager,
    McpConfig,
    McpServerConfig,
    McpServerSummary,
    McpSettings,
    load_mcp_config,
)
from .model_metadata import ModelMetadata, discover_model_metadata
from .models import ChatChunk, ChatMessage, TokenUsage, ToolCall, ToolCallDelta
from .session import (
    AgentsDocument,
    LoadedSession,
    SessionNotFoundError,
    SessionStore,
    SessionSummary,
    is_agents_md_message,
    load_agents_md,
)
from .skills import SkillInfo, SkillsCatalog
from .tools import TOOL_COUNT
from .tools.context import ContextArchiveToolset, ContextToolset
from .tools.external import ExternalFileToolset
from .tools.mcpconfig import McpConfigToolset
from .tools.registry import ToolRegistry
from .tools.skills import SkillToolset
from .tools.types import ToolResult, ToolSpec

__version__ = "0.1.0"

__all__ = [
    "AgentBusyError",
    "AgentEvent",
    "AgentLimits",
    "AgentsDocument",
    "AppConfig",
    "ArchivedContext",
    "ChatChunk",
    "ChatMessage",
    "CompactionResult",
    "CONFIG_TEMPLATE",
    "ConfigFileCreatedError",
    "ConfigurationError",
    "CONTEXT_WARNING_RATIO",
    "ContextArchive",
    "ContextArchiveToolset",
    "ContextLimitError",
    "ContextSettings",
    "ContextStatsUpdated",
    "ContextToolset",
    "ContextTracker",
    "ContextUsage",
    "ContextWarning",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_CONTEXT_WINDOW",
    "ExternalFileToolset",
    "FileAgent",
    "LLMSettings",
    "LoadedSession",
    "McpClientManager",
    "McpConfig",
    "McpConfigSettings",
    "McpConfigToolset",
    "McpLoadResult",
    "McpServerConfig",
    "McpServerSummary",
    "McpSettings",
    "ModelCallError",
    "ModelMetadata",
    "TOOL_COUNT",
    "RecallHit",
    "RecallResult",
    "SearchSettings",
    "SessionNotFoundError",
    "SessionSettings",
    "SessionStore",
    "SessionSummary",
    "SkillInfo",
    "SkillToolset",
    "SkillsCatalog",
    "TextDelta",
    "TokenUsage",
    "ToolCall",
    "ToolCallDelta",
    "ToolError",
    "ToolFinished",
    "ToolRegistry",
    "ToolResult",
    "ToolRoundLimitReached",
    "ToolSpec",
    "ToolStarted",
    "ToolsSettings",
    "TuiSettings",
    "TurnCompleted",
    "TurnFailed",
    "TurnStarted",
    "WorkspaceBoundaryError",
    "WorkspaceError",
    "XAgentError",
    "__version__",
    "build_agent",
    "discover_model_metadata",
    "estimate_messages_tokens",
    "estimate_text_tokens",
    "is_agents_md_message",
    "load_agents_md",
    "load_config",
    "load_mcp_config",
    "write_default_config",
]
