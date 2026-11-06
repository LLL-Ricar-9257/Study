"""组合根：把配置、LLM 适配器、文件工具、网页工具、搜索工具、命令行工具、
Skill 加载/创建、MCP 配置写入、上下文统计、历史归档检索以及 MCP Server 工具
组装成 Agent。

所有 UI（TUI / Web / Desktop）都调用同一个 build_agent，避免重复业务配置。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .agent import FileAgent
from .archive import ContextArchive
from .config import AppConfig
from .context import ContextTracker
from .llm.openai_chat import OpenAIChatModel
from .mcp import McpClientManager, McpSettings
from .model_metadata import ModelMetadata
from .prompts import build_system_prompt
from .session import SessionStore, load_agents_md
from .skills import SkillsCatalog, bundled_root
from .tools.context import ContextArchiveToolset, ContextToolset
from .tools.external import ExternalFileToolset
from .tools.filesystem import FileToolset
from .tools.mcpconfig import McpConfigToolset
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
    model_metadata: ModelMetadata | None = None,
    mcp_async: bool = True,
) -> FileAgent:
    """组装 Agent。

    ``project_root`` 默认为程序启动时的当前工作目录，用于发现
    ``.agents/skills`` 和 ``.mcp.json``；它独立于 ``config.workspace``。
    扫描顺序为启动目录 → 工作区 → xAgent 自带目录（源码仓库根下的
    ``.agents/skills``），同名 Skill 以先扫描到的为准。

    ``bundled_roots`` 覆盖自带 Skill 扫描根：默认只扫描 ``bundled_root()``，
    传空序列可禁用（便于测试构造隔离环境）。

    ``mcp_settings`` 覆盖 MCP 开关与超时；默认使用 ``config.mcp``。
    项目目录没有 ``.mcp.json`` 时不会创建任何 MCP 连接。``createMcp`` 也以
    ``mcp_settings.config_path`` 为目标，保证写入的就是下次启动会读取的那份文件。

    ``model_metadata`` 是启动时从模型上游读取的上下文窗口等信息。TUI 启动流程
    会先查询再传入；没有传入时按 ``config.context.default_window`` 计算占比。

    ``mcp_async=True``（默认）时不在这里连接 MCP，而是把管理器交给 Agent，
    由 UI 在首屏渲染后调用 :meth:`FileAgent.start_mcp` 后台加载并刷新界面；
    ``False`` 时保持同步连接，便于测试或非交互前端。
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

    effective_mcp = mcp_settings or config.mcp.to_settings()
    context_window = (
        model_metadata.context_window
        if model_metadata is not None and model_metadata.context_window is not None
        else 0
    )
    context_tracker = ContextTracker(
        context_window,
        default_window=config.context.default_window,
        warning_ratio=config.context.warning_ratio,
    )
    context_tools = ContextToolset(context_tracker)
    archive = ContextArchive(root, config.context.archive_dir)
    archive_tools = ContextArchiveToolset(
        archive,
        max_hits=config.context.max_recall_hits,
        max_chars=config.context.max_recall_chars,
    )
    file_tools = FileToolset(config.workspace, limits=config.limits)
    external_tools = ExternalFileToolset(limits=config.limits)
    web_tools = WebToolset(
        limits=config.limits,
        timeout_seconds=config.tools.web_timeout_seconds,
        max_download_bytes=config.tools.web_max_download_bytes,
    )
    search_tools = SearchToolset(
        config.search,
        limits=config.limits,
        max_response_bytes=config.tools.search_max_response_bytes,
    )
    runcli_tools = RunCliToolset(
        config.workspace,
        timeout_seconds=config.tools.runcli_timeout_seconds,
        max_output_chars=config.tools.runcli_max_output_chars,
    )
    skill_tools = SkillToolset(
        skills,
        project_root=root,
        workspace_root=config.workspace,
        limits=config.limits,
    )
    mcp_config_tools = McpConfigToolset(
        project_root=root,
        workspace_root=config.workspace,
        config_path=effective_mcp.config_path,
        limits=config.limits,
    )
    handlers = (
        file_tools.registry_handlers()
        | external_tools.registry_handlers()
        | context_tools.registry_handlers()
        | archive_tools.registry_handlers()
        | web_tools.registry_handlers()
        | search_tools.registry_handlers()
        | runcli_tools.registry_handlers()
        | skill_tools.registry_handlers()
        | mcp_config_tools.registry_handlers()
    )
    specs: list[ToolSpec] = list(ALL_TOOL_SPECS)

    mcp_manager = McpClientManager.from_project(
        root,
        settings=effective_mcp,
        reserved_names={spec.name for spec in ALL_TOOL_SPECS},
    )
    cleanup = []
    mcp_lines: tuple[str, ...] = ()
    startup_notices: tuple[str, ...] = ()
    if mcp_manager is not None:
        cleanup.append(mcp_manager.close)
        if mcp_async:
            startup_notices = ("MCP：正在后台连接服务器，连接状态会显示在侧栏。",)
        else:
            # 连接失败不会抛异常，只会体现在启动提示与系统提示词中。
            mcp_manager.start()
            specs.extend(mcp_manager.tool_specs())
            handlers = handlers | mcp_manager.registry_handlers()
            mcp_lines = mcp_manager.prompt_lines()
            startup_notices = mcp_manager.notices()

    def _load_agents_md():
        if not config.sessions.inject_agents_md:
            return None
        return load_agents_md(
            root,
            filename=config.sessions.agents_file,
            max_chars=config.sessions.max_agents_md_chars,
        )

    registry = ToolRegistry(handlers, specs)
    return FileAgent(
        model=OpenAIChatModel(config.llm),
        tools=registry,
        system_prompt=build_system_prompt(
            config.workspace,
            skills,
            mcp_lines=mcp_lines,
        ),
        max_tool_rounds=config.limits.max_tool_rounds,
        startup_notices=startup_notices,
        cleanup=cleanup,
        context=context_tracker,
        archive=archive,
        context_settings=config.context,
        session_store=SessionStore(root, config.sessions.directory),
        workspace=root,
        agents_md=_load_agents_md(),
        agents_md_loader=_load_agents_md,
        mcp_manager=mcp_manager if mcp_async else None,
    )
