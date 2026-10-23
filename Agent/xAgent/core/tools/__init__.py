"""工具基础设施与文件系统实现。"""

from .filesystem import FileToolset
from .registry import ToolRegistry
from .specs import FILE_TOOL_SPECS
from .types import ToolResult, ToolSpec

TOOL_COUNT = len(FILE_TOOL_SPECS)

__all__ = [
    "FILE_TOOL_SPECS",
    "TOOL_COUNT",
    "FileToolset",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
]
