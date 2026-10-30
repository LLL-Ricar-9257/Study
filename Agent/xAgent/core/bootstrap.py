"""组合根：把配置、LLM 适配器、文件工具、网页工具、搜索工具、命令行工具和 Skill 组装成 Agent。

所有 UI（TUI / Web / Desktop）都调用同一个 build_agent，避免重复业务配置。
"""

from __future__ import annotations

from pathlib import Path

from .agent import FileAgent
from .config import AppConfig
from .llm.openai_chat import OpenAIChatModel
from .prompts import build_system_prompt
from .skills import SkillsCatalog
from .tools.filesystem import FileToolset
from .tools.registry import ToolRegistry
from .tools.runcli import RunCliToolset
from .tools.search import SearchToolset
from .tools.skills import SkillToolset
from .tools.specs import ALL_TOOL_SPECS
from .tools.web import WebToolset


def build_agent(
    config: AppConfig,
    *,
    project_root: str | Path | None = None,
) -> FileAgent:
    """组装 Agent。

    ``project_root`` 默认为程序启动时的当前工作目录，用于发现
    ``.agents/skills``；它独立于 ``config.workspace``。为兼容从其他目录
    启动并通过 ``--workspace`` 指定工作区的场景，也会扫描工作区内的
    ``.agents/skills``；同名 Skill 以启动目录优先。
    """
    root = (
        Path.cwd().resolve()
        if project_root is None
        else Path(project_root).expanduser().resolve()
    )
    skills = SkillsCatalog.discover(root, extra_roots=(config.workspace,))

    file_tools = FileToolset(config.workspace, limits=config.limits)
    web_tools = WebToolset(limits=config.limits)
    search_tools = SearchToolset(config.search, limits=config.limits)
    runcli_tools = RunCliToolset(config.workspace)
    skill_tools = SkillToolset(
        skills,
        project_root=root,
        workspace_root=config.workspace,
        limits=config.limits,
    )
    handlers = (
        file_tools.registry_handlers()
        | web_tools.registry_handlers()
        | search_tools.registry_handlers()
        | runcli_tools.registry_handlers()
        | skill_tools.registry_handlers()
    )
    registry = ToolRegistry(handlers, ALL_TOOL_SPECS)
    return FileAgent(
        model=OpenAIChatModel(config.llm),
        tools=registry,
        system_prompt=build_system_prompt(config.workspace, skills),
        max_tool_rounds=config.limits.max_tool_rounds,
    )
