"""工具基础设施、文件系统、网页、搜索、命令行与 Skill 加载实现。"""

from .filesystem import FileToolset
from .registry import ToolRegistry
from .runcli import RunCliToolset
from .search import SearchToolset
from .skills import SkillToolset
from .specs import (
    ALL_TOOL_SPECS,
    FILE_TOOL_SPECS,
    RUNCLI_TOOL_SPECS,
    SEARCH_TOOL_SPECS,
    SKILL_TOOL_SPECS,
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
    "SKILL_TOOL_SPECS",
    "TOOL_COUNT",
    "WEB_TOOL_SPECS",
    "FileToolset",
    "RunCliToolset",
    "SearchToolset",
    "SkillToolset",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "WebToolset",
]
