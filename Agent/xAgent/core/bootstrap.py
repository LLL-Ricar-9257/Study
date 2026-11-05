"""组合根：把配置、LLM 适配器、文件工具、网页工具、搜索工具、命令行工具、
Skill 加载/创建以及 MCP Server 工具组装成 Agent。

所有 UI（TUI / Web / Desktop）都调用同一个 build_agent，避免重复业务配置。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .agent import FileAgent
from .config import AppConfig
from .llm.openai_chat import OpenAIChatModel
from .mcp import McpClientManager, McpSettings
from .prompts import build_system_prompt
from .skills import SkillsCatalog, bundled_root
from .tools.external import ExternalFileToolset
from .tools.filesystem import FileToolset
from .tools.registry import ToolRegistry
from .tools.runcli import RunCliToolset
from .tools.search import SearchToolset
from .tools.skills import SkillToolset
from .tools.specs import ALL_TOOL_SPECS
from .tools.types import ToolSpec
from .tools.web import WebToolset


def build_agent(
    config: AppConfig,
    *,
    project_root: str | Path | None = None,
    bundled_roots: Iterable[str | Path] | None = None,
    mcp_settings: McpSettings | None = None,
) -> FileAgent:
    """组装 Agent。

    ``project_root`` 默认为程序启动时的当前工作目录，用于发现
    ``.agents/skills`` 和 ``.mcp.json``；它独立于 ``config.workspace``。
    扫描顺序为启动目录 → 工作区 → xAgent 自带目录（源码仓库根下的
    ``.agents/skills``），同名 Skill 以先扫描到的为准。

    ``bundled_roots`` 覆盖自带 Skill 扫描根：默认只扫描 ``bundled_root()``，
    传空序列可禁用（便于测试构造隔离环境）。

    ``mcp_settings`` 覆盖 MCP 开关与超时；默认读取 ``XAGENT_MCP_*`` 环境变量。
    项目目录没有 ``.mcp.json`` 时不会创建任何 MCP 连接。
    """
    root = (
        Path.cwd().resolve()
        if project_root is None
        else Path(project_root).expanduser().resolve()
    )
    bundled = (bundled_root(),) if bundled_roots is None else tuple(bundled_roots)
    skills = SkillsCatalog.discover(
        root,
        extra_roots=(config.workspace,),
        bundled_roots=bundled,
    )

    file_tools = FileToolset(config.workspace, limits=config.limits)
    external_tools = ExternalFileToolset(limits=config.limits)
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
        | external_tools.registry_handlers()
        | web_tools.registry_handlers()
        | search_tools.registry_handlers()
        | runcli_tools.registry_handlers()
        | skill_tools.registry_handlers()
    )
    specs: list[ToolSpec] = list(ALL_TOOL_SPECS)

    mcp_manager = McpClientManager.from_project(
        root,
        settings=mcp_settings or McpSettings.from_environment(),
        reserved_names={spec.name for spec in ALL_TOOL_SPECS},
    )
    cleanup = []
    if mcp_manager is not None:
        # 连接失败不会抛异常，只会体现在启动提示与系统提示词中。
        mcp_manager.start()
        specs.extend(mcp_manager.tool_specs())
        handlers = handlers | mcp_manager.registry_handlers()
        cleanup.append(mcp_manager.close)

    registry = ToolRegistry(handlers, specs)
    return FileAgent(
        model=OpenAIChatModel(config.llm),
        tools=registry,
        system_prompt=build_system_prompt(
            config.workspace,
            skills,
            mcp_lines=mcp_manager.prompt_lines() if mcp_manager else (),
        ),
        max_tool_rounds=config.limits.max_tool_rounds,
        startup_notices=mcp_manager.notices() if mcp_manager else (),
        cleanup=cleanup,
    )
