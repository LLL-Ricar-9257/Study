"""组合根：把配置、LLM 适配器、文件工具组装成 Agent。

所有 UI（TUI / Web / Desktop）都调用同一个 build_agent，避免重复业务配置。
"""

from __future__ import annotations

from .agent import FileAgent
from .config import AppConfig
from .llm.openai_chat import OpenAIChatModel
from .prompts import build_system_prompt
from .tools.filesystem import FileToolset
from .tools.registry import ToolRegistry
from .tools.specs import FILE_TOOL_SPECS


def build_agent(config: AppConfig) -> FileAgent:
    toolset = FileToolset(config.workspace, limits=config.limits)
    registry = ToolRegistry(toolset.registry_handlers(), FILE_TOOL_SPECS)
    return FileAgent(
        model=OpenAIChatModel(config.llm),
        tools=registry,
        system_prompt=build_system_prompt(config.workspace),
        max_tool_rounds=config.limits.max_tool_rounds,
    )
