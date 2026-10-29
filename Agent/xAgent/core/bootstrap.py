"""组合根：把配置、LLM 适配器、文件工具、网页工具和搜索工具组装成 Agent。

所有 UI（TUI / Web / Desktop）都调用同一个 build_agent，避免重复业务配置。
"""

from __future__ import annotations

from .agent import FileAgent
from .config import AppConfig
from .llm.openai_chat import OpenAIChatModel
from .prompts import build_system_prompt
from .tools.filesystem import FileToolset
from .tools.registry import ToolRegistry
from .tools.search import SearchToolset
from .tools.specs import ALL_TOOL_SPECS
from .tools.web import WebToolset


def build_agent(config: AppConfig) -> FileAgent:
    file_tools = FileToolset(config.workspace, limits=config.limits)
    web_tools = WebToolset(limits=config.limits)
    search_tools = SearchToolset(config.search, limits=config.limits)
    handlers = (
        file_tools.registry_handlers()
        | web_tools.registry_handlers()
        | search_tools.registry_handlers()
    )
    registry = ToolRegistry(handlers, ALL_TOOL_SPECS)
    return FileAgent(
        model=OpenAIChatModel(config.llm),
        tools=registry,
        system_prompt=build_system_prompt(config.workspace),
        max_tool_rounds=config.limits.max_tool_rounds,
    )
