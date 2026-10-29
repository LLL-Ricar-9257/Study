"""工具基础设施、文件系统实现、网页抓取实现、网络搜索实现与本地命令行实现。"""

from .filesystem import FileToolset
from .registry import ToolRegistry
from .runcli import RunCliToolset
from .search import SearchToolset
from .specs import (
    ALL_TOOL_SPECS,
    FILE_TOOL_SPECS,
    RUNCLI_TOOL_SPECS,
    SEARCH_TOOL_SPECS,
    WEB_TOOL_SPECS,
)
from .types import ToolResult, ToolSpec
from .web import WebToolset

TOOL_COUNT = len(ALL_TOOL_SPECS)

__all__ = [
    "ALL_TOOL_SPECS",
    "FILE_TOOL_SPECS",
    "RUNCLI_TOOL_SPECS",
    "SEARCH_TOOL_SPECS",
    "TOOL_COUNT",
    "WEB_TOOL_SPECS",
    "FileToolset",
    "RunCliToolset",
    "SearchToolset",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "WebToolset",
]
