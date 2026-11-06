"""Textual 表现层。

它只做三件事：采集输入、消费 core 事件、渲染状态。
不包含任何文件操作或模型调用逻辑，方便未来替换为 Web / Desktop 前端。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from textual import events
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Header, Input, OptionList, Static

from core import (
    TOOL_COUNT,
    AgentEvent,
    AppConfig,
    ContextStatsUpdated,
    ContextWarning,
    FileAgent,
    ModelMetadata,
    TextDelta,
    ToolFinished,
    ToolRoundLimitReached,
    ToolStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    XAgentError,
    is_agents_md_message,
)

from .formatting import (
    compact_tokens,
    configure_display_limits,
    context_detail_text,
    mcp_sidebar_text,
    session_info_text,
    sidebar_context_text,
    status_text,
)
from .palette import ChineseCommandPalette
from .slash import (
    SlashCommand,
    build_template,
    filter_commands,
    find_command,
    help_lines,
)
from .terminal import console_is_active
from .widgets import (
    AssistantMessage,
    CompactionPanel,
    ContextBar,
    ErrorMessage,
    NoticeMessage,
    PromptInput,
    SessionPanel,
    SlashMenu,
    SystemPromptCard,
    ToolActivity,
    UserMessage,
)

DEFAULT_PLACEHOLDER = " 向 xAgent 描述任务，Enter 发送..."
BLOCKED_PLACEHOLDER = " 上下文接近上限，请先执行 /compact 压缩上下文"

HELP_TEXT = f"""命令
  输入 / 打开工具菜单；继续输入可过滤，↑↓ 选择，Enter 确认，Esc 关闭。

{help_lines()}

快捷键
  Esc           关闭菜单 / 取消本轮生成
  Ctrl+L        清空对话
  Ctrl+V        粘贴系统剪贴板文本
  Ctrl+P        打开命令面板
  Ctrl+Q        退出

其它
  直接输入自然语言，Enter 发送。
  上下文达到模型窗口阈值时，请先执行 /compact 压缩上下文。
  文件操作被限制在当前工作区内。"""


class FileAgentApp(App[None]):
    TITLE = "xAgent | 本地文件管理"
    SUB_TITLE = ""
    CSS_PATH = Path(__file__).with_name("styles.tcss")

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", show=True),
        Binding("escape", "cancel_turn", "取消本轮", show=True),
        Binding("ctrl+l", "clear_chat", "清空", show=True),
    ]

    COMMAND_PALETTE_DISPLAY = "Ctrl+P"

    def __init__(
        self,
        agent: FileAgent,
        config: AppConfig,
        model_metadata: ModelMetadata | None = None,
    ) -> None:
        super().__init__()
        configure_display_limits(
            max_tool_output=config.tui.max_tool_output_chars,
            max_tool_arguments=config.tui.max_tool_arguments_chars,
        )
        self.agent = agent
        self.config = config
        self.model_metadata = model_metadata
        self._turn_active = False
        self._turn_had_text = False
        self._turn_worker: Any = None
        self._streaming: AssistantMessage | None = None
        self._phase_text = ""
        self._last_paint = 0.0
        self._tool_cards: dict[str, ToolActivity] = {}
        self._state = "就绪"
        self._agent_closed = False
        self._follow_transcript_tail = True
        self._reflow_scheduled = False
        self._native_focus: bool | None = None
        self._mcp_summaries = agent.mcp_summaries
        self._mcp_pending = agent.mcp_pending
        self._mcp_error = ""

    # -- 布局 -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="shell"):
            with VerticalScroll(id="sidebar"):
                yield Static("xAgent", classes="brand")
                yield Static("LOCAL FILE AGENT", classes="tagline")
                yield Static(
                    "模型\n" + self.config.llm.model,
                    id="sidebar-model",
                    classes="side-block",
                    markup=False,
                )
                yield Static(
                    self._context_window_label(),
                    id="sidebar-context",
                    classes="side-block",
                    markup=False,
                )
                yield Static(
                    mcp_sidebar_text((), pending=self._mcp_pending),
                    id="sidebar-mcp",
                    classes="side-block",
                    markup=False,
                )
                yield Static(
                    "工作区\n" + str(self.config.workspace),
                    id="sidebar-workspace",
                    classes="side-block",
                    markup=False,
                )
                yield Static(
                    f"内置工具\n{TOOL_COUNT} 项能力\n"
                    "文件 / 外部读取 / 网页 / 搜索 / 命令行 / 技能 / 上下文",
                    classes="side-block",
                    markup=False,
                )
                yield Static(
                    "快捷键\n/  工具菜单\nEsc  取消生成\n"
                    "Ctrl+V  粘贴\nCtrl+L  清空\nCtrl+P  命令\nCtrl+Q  退出",
                    classes="side-block",
                    markup=False,
                )
            with Vertical(id="main"):
                yield VerticalScroll(id="transcript")
                yield SlashMenu()
                with Horizontal(id="composer"):
                    yield Static(">", id="prompt-prefix", markup=False)
                    yield PromptInput(
                        placeholder=DEFAULT_PLACEHOLDER,
                        id="prompt",
                        compact=True,
                    )
                yield ContextBar()
                with Horizontal(id="composer-footer"):
                    yield Static(
                        "Enter 发送   |   Esc 取消   |   Ctrl+V 粘贴",
                        id="hint",
                        markup=False,
                    )
                    yield Static("", id="status", markup=False)

    async def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()
        self._set_status("就绪")
        self._refresh_sidebar()
        self._refresh_context()
        await self._mount_system_prompt(collapsed=True)
        for notice in self.agent.startup_notices:
            await self._append(NoticeMessage(notice))
        await self._append_agents_notice()
        if self.model_metadata is not None:
            await self._append(NoticeMessage(self._model_metadata_notice()))
        self._native_focus = console_is_active()
        if self._native_focus is not None:
            # conhost 不上报 AppFocus，轮询窗口前台状态作为兜底。
            self.set_interval(0.5, self._poll_native_focus)
        if self.agent.mcp_pending:
            # 先渲染 TUI，再在后台线程连接 MCP；完成后只刷新侧栏区域。
            self.run_worker(
                self._load_mcp_worker(),
                name="mcp-load",
                group="mcp",
                exclusive=False,
            )

    async def on_unmount(self) -> None:
        if self._agent_closed:
            return
        try:
            await self.agent.aclose()
            self._agent_closed = True
        except Exception:
            # 退出阶段不阻塞 UI，也不向用户抛错。
            pass

    # -- 窗口焦点 / 尺寸恢复 ----------------------------------------------

    def on_app_blur(self, event: events.AppBlur) -> None:
        """记住失去焦点前用户是否停留在会话底部。"""
        try:
            transcript = self.query_one("#transcript", VerticalScroll)
            self._follow_transcript_tail = transcript.is_vertical_scroll_end
        except Exception:
            self._follow_transcript_tail = True

    def on_app_focus(self, event: events.AppFocus) -> None:
        """重新获得焦点时强制重排，修复后台期间留下的错位。"""
        self._schedule_transcript_reflow()

    def on_resize(self, event: events.Resize) -> None:
        self._schedule_transcript_reflow()
        self._refresh_context()

    def _poll_native_focus(self) -> None:
        active = console_is_active()
        if active is None:
            return
        previous = self._native_focus
        self._native_focus = active
        if active and previous is False:
            self._schedule_transcript_reflow()

    def _schedule_transcript_reflow(self) -> None:
        if self._reflow_scheduled:
            return
        self._reflow_scheduled = True
        self.call_after_refresh(self._reflow_transcript)

    def _reflow_transcript(self) -> None:
        self._reflow_scheduled = False
        try:
            self.screen.refresh(repaint=True, layout=True)
            transcript = self.query_one("#transcript", VerticalScroll)
            for child in transcript.children:
                child.refresh(layout=True)
            transcript.refresh(layout=True)
            if self._follow_transcript_tail:
                self._scroll_end()
        except Exception:
            # 面板切换期间组件可能暂时不可用；下一帧会再次尝试。
            pass

    # -- 命令面板 ---------------------------------------------------------

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """只提供中文命令，避免 Textual 内置英文项混入。"""
        yield SystemCommand(
            "显示命令帮助",
            "查看 xAgent 的可用命令和快捷键",
            self._command_help,
        )
        yield SystemCommand(
            "查看系统提示词",
            "展开或收起模型本轮实际使用的系统提示词",
            self.action_toggle_system_prompt,
        )
        yield SystemCommand(
            "清空对话",
            "清空当前会话和界面内容",
            self.action_clear_chat,
        )
        yield SystemCommand(
            "取消当前生成",
            "停止正在生成或执行的回复",
            self.action_cancel_turn,
        )
        yield SystemCommand(
            "查看当前工作区",
            "显示 xAgent 可以访问的目录",
            self._command_workspace,
        )
        yield SystemCommand(
            "新建会话",
            "保存当前会话并开始一个新会话",
            self._command_new,
        )
        yield SystemCommand(
            "恢复历史会话",
            "打开会话列表，选择后恢复对话",
            self._command_resume,
        )
        yield SystemCommand(
            "保存当前会话",
            "把当前会话写入 .agents/sessions 事件日志",
            self._command_save,
        )
        yield SystemCommand(
            "压缩上下文",
            "归档当前上下文并生成摘要，释放上下文空间",
            self._start_compact,
        )
        yield SystemCommand(
            "查看上下文用量",
            "显示 token、上下文窗口占用与缓存命中数据",
            self._command_context,
        )
        yield SystemCommand(
            "聚焦输入框",
            "把键盘焦点移回底部输入框",
            self._focus_prompt,
        )
        yield SystemCommand(
            "退出 xAgent",
            "关闭当前会话并退出程序",
            self.action_quit,
        )

    def action_command_palette(self) -> None:
        """打开中文命令面板。"""
        if self.use_command_palette and not ChineseCommandPalette.is_open(self):
            self.push_screen(ChineseCommandPalette(id="--command-palette"))

    async def _command_help(self) -> None:
        await self._append(NoticeMessage(HELP_TEXT))

    async def _command_workspace(self) -> None:
        await self._append(NoticeMessage(f"当前工作区：{self.config.workspace}"))

    async def _command_context(self) -> None:
        lines = [context_detail_text(self.agent.context_usage)]
        archive = self.agent.archive
        if archive is not None:
            lines.append(f"归档目录：{archive.directory}")
            lines.append("压缩后会在此目录生成 <编号>.jsonl，可用 recallContext 检索。")
        await self._append(NoticeMessage("\n".join(lines)))

    async def _command_cost(self) -> None:
        usage = self.agent.context_usage
        lines = [
            "会话消耗",
            f"请求：{usage.requests} 次",
            (
                f"输入：{usage.total_prompt_tokens:,} tok；"
                f"输出：{usage.total_completion_tokens:,} tok"
            ),
        ]
        hit_rate = usage.cache_hit_rate
        if usage.cache_reports and hit_rate is not None:
            lines.append(
                f"缓存：命中 {usage.total_cached_tokens:,} tok，未命中 "
                f"{usage.total_cache_miss_tokens:,} tok，命中率 "
                f"{hit_rate * 100:.1f}%"
            )
        else:
            lines.append("缓存：模型供应商未返回 Prompt 缓存数据")
        lines.append("输入 /context 查看上下文窗口与最近一次请求的明细。")
        await self._append(NoticeMessage("\n".join(lines)))

    async def _command_model(self) -> None:
        llm = self.config.llm
        metadata = self.model_metadata
        if metadata is None:
            context_line = (
                "上下文窗口：未查询上游（按默认 "
                f"{compact_tokens(self.config.context.default_window)} 计算）"
            )
        elif metadata.context_window is not None:
            context_line = (
                f"上下文窗口：{metadata.context_window:,} tok（启动时从上游获取）"
            )
        else:
            context_line = (
                f"上下文窗口：未知（{metadata.error or '上游未返回'}）；"
                "界面按默认 "
                f"{compact_tokens(self.config.context.default_window)} 计算占比"
            )
        lines = [
            "模型信息",
            f"模型：{llm.model}",
            f"接口：{llm.base_url}",
            context_line,
        ]
        if metadata is not None and metadata.display_name:
            lines.insert(2, f"上游名称：{metadata.display_name}")
        if metadata is not None and metadata.endpoint:
            lines.append(f"上游接口：{metadata.endpoint}")
        if metadata is not None and metadata.max_output_tokens is not None:
            lines.append(f"最大输出：{metadata.max_output_tokens:,} tok")
        await self._append(NoticeMessage("\n".join(lines)))

    async def _command_tools(self) -> None:
        specs = self.agent.tool_specs
        lines = [f"可用工具（{len(specs)}）"]
        for spec in specs:
            description = " ".join(spec.description.split())
            if len(description) > 72:
                description = description[:72] + "..."
            lines.append(f"- {spec.name}：{description}")
        await self._append(NoticeMessage("\n".join(lines)))

    def _refresh_context(self) -> None:
        usage = self.agent.context_usage
        try:
            bar = self.query_one(ContextBar)
            # 用状态栏自身的宽度判断是否隐藏次要字段；首帧布局前回退到
            # 屏幕宽度减侧栏宽度。
            width = bar.size.width or max(self.size.width - 34, 0)
            bar.show(usage, width)
        except Exception:
            pass
        try:
            self.query_one("#sidebar-context", Static).update(
                sidebar_context_text(usage)
            )
        except Exception:
            pass
        self._apply_input_state(usage)

    def _apply_input_state(self, usage: Any) -> None:
        """达到 80% 时把占位文本换成红色提示，但保留输入框可用（/compact 要被接受）。"""

        try:
            prompt = self.query_one("#prompt", Input)
        except NoMatches:
            return
        if self._turn_active:
            return
        if usage.needs_compaction:
            prompt.placeholder = BLOCKED_PLACEHOLDER
            prompt.add_class("context-blocked")
        else:
            prompt.placeholder = DEFAULT_PLACEHOLDER
            prompt.remove_class("context-blocked")

    def _refresh_mcp(self) -> None:
        try:
            self.query_one("#sidebar-mcp", Static).update(
                mcp_sidebar_text(
                    self._mcp_summaries,
                    pending=self._mcp_pending,
                    max_tools=self.config.tui.mcp_max_tools,
                )
            )
        except Exception:
            pass

    async def _load_mcp_worker(self) -> None:
        """TUI 首屏渲染后连接 MCP，完成后只刷新侧栏。"""

        try:
            result = await self.agent.start_mcp()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 后台加载失败不能影响界面
            self._mcp_error = f"{type(exc).__name__}: {exc}"
            result = None
        self._mcp_pending = False
        if result is not None:
            self._mcp_summaries = tuple(result.summaries)
            if result.error and not self._mcp_error:
                self._mcp_error = result.error
        self._refresh_mcp()
        if self._mcp_error:
            await self._append(ErrorMessage(f"MCP 加载失败：{self._mcp_error}"))
            return
        if result is not None:
            for notice in result.notices:
                await self._append(NoticeMessage(notice))
            if result.tool_count:
                self.notify(
                    f"MCP 已连接，注册 {result.tool_count} 个工具。",
                    severity="information",
                )

    async def _mount_system_prompt(self, *, collapsed: bool = False) -> None:
        card = SystemPromptCard(self.agent.system_prompt, collapsed=collapsed)
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(card)
        transcript.scroll_home(animate=False)

    async def action_toggle_system_prompt(self) -> None:
        """展开或收起系统提示词；若已被清空则重新显示。"""
        try:
            card = self.query_one(SystemPromptCard)
        except NoMatches:
            await self._mount_system_prompt(collapsed=False)
            return
        card.collapsed = not card.collapsed
        card.scroll_visible(animate=False)

    def _focus_prompt(self) -> None:
        self.query_one("#prompt", Input).focus()

    def _context_window_label(self) -> str:
        metadata = self.model_metadata
        if metadata is not None and metadata.context_window is not None:
            return (
                "上下文\n窗口 "
                f"{compact_tokens(metadata.context_window)} tok（上游获取）"
            )
        # 首帧还没有消息快照，先展示默认窗口；挂载后由 _refresh_context 换成
        # 「当前大小 / 窗口 / 占比」的实时文本。
        return (
            "上下文\n窗口 "
            f"{compact_tokens(self.config.context.default_window)} tok（默认）"
        )

    def _model_metadata_notice(self) -> str:
        metadata = self.model_metadata
        default_text = compact_tokens(self.config.context.default_window)
        if metadata is None:
            return (
                "模型上下文窗口：未查询上游，界面按默认 "
                f"{default_text} 计算（标记「默认」）"
            )
        if metadata.context_window is not None:
            name = f"（{metadata.display_name}）" if metadata.display_name else ""
            return (
                f"模型上下文窗口：{metadata.context_window:,} tok{name}\n"
                f"来源：启动时从上游 {metadata.endpoint} 获取"
            )
        return (
            "未能从当前模型上游获取上下文窗口："
            f"{metadata.error or '上游未返回'}。\n"
            f"界面将按默认 {default_text} 计算占比，并标记「默认」。"
        )

    def _refresh_sidebar(self) -> None:
        try:
            self.query_one("#sidebar-model", Static).update(
                "模型\n" + self.config.llm.model
            )
            self.query_one("#sidebar-context", Static).update(
                sidebar_context_text(self.agent.context_usage)
            )
            self.query_one("#sidebar-workspace", Static).update(
                "工作区\n" + str(self.config.workspace)
            )
        except Exception:
            pass
        self._refresh_mcp()

    # -- 斜杠工具菜单 -----------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "prompt":
            return
        self._update_slash_menu(event.value)

    def _update_slash_menu(self, value: str) -> None:
        try:
            menu = self.query_one(SlashMenu)
        except NoMatches:
            return
        if not value.startswith("/") or " " in value or "\t" in value:
            menu.hide()
            return
        menu.show_commands(filter_commands(value[1:]))

    def _hide_slash_menu(self) -> None:
        try:
            self.query_one(SlashMenu).hide()
        except NoMatches:
            pass

    def _slash_menu_visible(self) -> bool:
        try:
            return self.query_one(SlashMenu).is_open
        except NoMatches:
            return False

    def slash_move(self, delta: int) -> None:
        """供 PromptInput 的上下键调用，在菜单可见时移动高亮项。"""

        try:
            menu = self.query_one(SlashMenu)
        except NoMatches:
            return
        if menu.is_open:
            menu.move(delta)

    async def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if event.option_list.id != "slash-options":
            return
        event.stop()
        command = find_command(event.option.id or "")
        if command is not None:
            await self._activate_slash_command(command)
        self._focus_prompt()

    async def _activate_slash_command(self, command: SlashCommand) -> None:
        self._hide_slash_menu()
        try:
            prompt = self.query_one("#prompt", Input)
        except NoMatches:
            return
        if command.kind == "compose":
            prompt.value = f"/{command.name} "
            prompt.focus()
            self.notify(
                f"已选择「{command.title}」，输入内容后按 Enter。",
                severity="information",
            )
            return
        prompt.value = ""
        await self._handle_command(f"/{command.name}")

    # -- 输入与命令 -------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        selected: str | None = None
        if text.startswith("/") and " " not in text and self._slash_menu_visible():
            try:
                selected = self.query_one(SlashMenu).highlighted_name()
            except NoMatches:
                selected = None
        event.input.value = ""
        if not text:
            return

        if text.lower() in {"exit", "quit", "退出"}:
            self.exit()
            return

        # 完整命令名优先于菜单高亮项，避免过滤事件尚未处理时选错命令。
        if text.startswith("/") and " " not in text:
            exact = find_command(text)
            if exact is not None:
                await self._activate_slash_command(exact)
                return

        if selected:
            command = find_command(selected)
            if command is not None:
                await self._activate_slash_command(command)
                return

        if text.startswith("/"):
            await self._handle_command(text)
            return

        if self._turn_active:
            self.notify("上一轮仍在处理中，请稍候或按 Esc 取消。", severity="warning")
            return

        if self.agent.context_limit_reached:
            # 输入框仍然可用，因为 /compact 必须能被接受；只拦截普通提示词。
            ratio = self.agent.context_usage.warning_ratio
            self.notify(
                f"上下文已达 {ratio:.0%}，请先执行 /compact。",
                severity="warning",
            )
            await self._append(
                NoticeMessage(
                    f"上下文已达到窗口的 {ratio:.0%}，"
                    "已暂停接受新的普通提示词。\n"
                    "请执行 /compact 压缩上下文；压缩成功后会自动恢复输入。"
                )
            )
            self._refresh_context()
            return

        self._turn_worker = self.run_worker(
            self._run_turn(text),
            name="agent-turn",
            group="agent",
            exclusive=True,
        )

    async def _handle_command(self, text: str) -> None:
        raw_name, _, argument = text.partition(" ")
        command = find_command(raw_name)
        if command is None:
            await self._append(
                NoticeMessage(f"未知命令：{raw_name}。输入 /help 查看可用命令。")
            )
            return

        if command.name == "help":
            await self._command_help()
        elif command.name == "new":
            await self._command_new()
        elif command.name == "resume":
            await self._command_resume()
        elif command.name == "save":
            await self._command_save()
        elif command.name == "session":
            await self._command_session()
        elif command.name == "rename":
            await self._command_rename(argument)
        elif command.name == "mcp":
            await self._command_mcp()
        elif command.name == "agents":
            await self._command_agents()
        elif command.name == "clear":
            await self.action_clear_chat()
        elif command.name == "system":
            await self.action_toggle_system_prompt()
        elif command.name == "workspace":
            await self._command_workspace()
        elif command.name == "context":
            await self._command_context()
        elif command.name == "cost":
            await self._command_cost()
        elif command.name == "model":
            await self._command_model()
        elif command.name == "tools":
            await self._command_tools()
        elif command.name == "compact":
            self._start_compact()
        elif command.name == "refine":
            draft = argument.strip()
            if not draft:
                self.notify(
                    "用法：/refine <要润色的提示词>。也可以输入 / 后选择「润色提示词」。",
                    severity="warning",
                )
                return
            self._start_refine(draft)
        elif command.name in {"review", "init"}:
            template = build_template(command, argument)
            if template is not None:
                prompt = self.query_one("#prompt", Input)
                prompt.value = template
                prompt.focus()
                self.notify(
                    f"已填入「{command.title}」提示词，可编辑后按 Enter 发送。",
                    severity="information",
                )
        elif command.name == "cancel":
            self.action_cancel_turn()
        elif command.name == "quit":
            await self.action_quit()

    # -- 会话管理 ---------------------------------------------------------

    async def _command_new(self) -> None:
        if self._turn_active:
            self.notify("请先等待本轮结束，或按 Esc 取消。", severity="warning")
            return
        try:
            saved = self.agent.new_session(
                save_current=self.config.sessions.auto_save_on_new
            )
        except XAgentError as exc:
            self.notify(str(exc), severity="error")
            return
        await self._reset_transcript()
        if saved is not None:
            await self._append(
                NoticeMessage(
                    f"已保存会话 {saved.session_id}（{saved.title}）。\n"
                    f"已新建会话 {self.agent.session_id}。"
                )
            )
        else:
            await self._append(
                NoticeMessage(
                    f"已新建会话 {self.agent.session_id or '（未启用会话存储）'}。"
                )
            )
        await self._append_agents_notice()
        self._set_status("新会话")

    async def _command_save(self) -> None:
        if self._turn_active:
            self.notify("请先等待本轮结束，或按 Esc 取消。", severity="warning")
            return
        try:
            summary = self.agent.save_session()
        except (XAgentError, ValueError) as exc:
            await self._append(ErrorMessage(f"保存会话失败：{exc}"))
            return
        if summary is None:
            await self._append(NoticeMessage("当前还没有可保存的对话内容。"))
            return
        await self._append(
            NoticeMessage(
                f"会话已保存：{summary.session_id}\n"
                f"标题：{summary.title}\n"
                f"消息：{summary.message_count} 条\n"
                f"日志：{summary.path}"
            )
        )
        self._set_status("已保存")

    async def _command_session(self) -> None:
        archive = self.agent.archive
        store = self.agent.session_store
        agents_md = self.agent.agents_md
        path = ""
        if store is not None and self.agent.session_id:
            path = str(store.directory / f"{self.agent.session_id}.jsonl")
        lines = [
            session_info_text(
                session_id=self.agent.session_id,
                title=self.agent.session_title,
                message_count=len(self.agent.messages),
                path=path,
                agents_md=str(agents_md.path) if agents_md else "",
            )
        ]
        if store is not None:
            lines.append(f"会话目录：{store.directory}")
        if archive is not None:
            lines.append(f"归档目录：{archive.directory}")
        await self._append(NoticeMessage("\n".join(lines)))

    async def _command_rename(self, argument: str) -> None:
        title = argument.strip()
        if not title:
            self.notify("用法：/rename <新的会话标题>", severity="warning")
            return
        try:
            self.agent.rename_session(title)
            saved = self.agent.save_session()
        except (ValueError, XAgentError) as exc:
            self.notify(str(exc), severity="error")
            return
        suffix = f"已保存到 {saved.session_id}。" if saved is not None else ""
        await self._append(NoticeMessage(f"会话标题已改为：{title}。{suffix}"))
        self._set_status("已重命名")

    async def _command_resume(self) -> None:
        if self._turn_active:
            self.notify("请先等待本轮结束，或按 Esc 取消。", severity="warning")
            return
        store = self.agent.session_store
        if store is None:
            await self._append(ErrorMessage("未配置会话目录，无法恢复会话。"))
            return
        sessions = store.list_sessions(limit=self.config.sessions.max_list)
        if not sessions:
            await self._append(NoticeMessage("还没有已保存的会话。先使用 /save 保存。"))
            return
        self.push_screen(SessionPanel(sessions), callback=self._resume_selected)

    def _resume_selected(self, session_id: str | None) -> None:
        if not session_id:
            return
        self.run_worker(
            self._resume_session_worker(session_id),
            name="session-resume",
            group="session",
            exclusive=False,
        )

    async def _resume_session_worker(self, session_id: str) -> None:
        try:
            loaded = self.agent.resume_session(session_id)
        except (XAgentError, ValueError) as exc:
            await self._append(ErrorMessage(f"恢复会话失败：{exc}"))
            return
        await self._render_history()
        await self._append(
            NoticeMessage(
                f"已恢复会话 {loaded.session_id}（{loaded.title}）。\n"
                f"消息：{len(loaded.messages)} 条"
            )
        )
        self._refresh_context()
        self._set_status("已恢复")

    async def _command_mcp(self) -> None:
        lines = ["MCP 状态"]
        if self._mcp_pending:
            lines.append("连接中：后台正在连接 MCP Server，稍后再试。")
        if self._mcp_error:
            lines.append(f"错误：{self._mcp_error}")
        if not self._mcp_summaries:
            if not self._mcp_pending and not self._mcp_error:
                lines.append(
                    "没有配置 MCP Server（项目目录的 .mcp.json 为空或不存在）。"
                )
        for summary in self._mcp_summaries:
            if summary.connected:
                lines.append(
                    f"{summary.name}（{summary.transport}）：已连接，"
                    f"{summary.tool_count} 个工具"
                )
                for tool_name in summary.tool_names:
                    lines.append(f"  - {tool_name}")
            else:
                lines.append(
                    f"{summary.name}（{summary.transport}）：连接失败"
                    f"：{summary.error}"
                )
        await self._append(NoticeMessage("\n".join(lines)))

    async def _command_agents(self) -> None:
        document = self.agent.agents_md
        if document is None:
            await self._append(
                NoticeMessage(
                    f"当前目录没有 {self.config.sessions.agents_file}，"
                    "或已在配置中关闭注入。"
                )
            )
            return
        preview = "\n".join(document.content.splitlines()[:12])
        lines = [
            "项目指令文件",
            f"路径：{document.path}",
            f"字符数：{len(document.content)}"
            + ("（已截断）" if document.truncated else ""),
            "注入方式：会话首次加载时注入一次，之后随会话日志复用。",
            "",
            preview,
        ]
        await self._append(NoticeMessage("\n".join(lines)))

    # -- 一轮对话 ---------------------------------------------------------

    async def _run_turn(self, text: str) -> None:
        self._begin_turn()
        await self._append(UserMessage(text))
        try:
            async for event in self.agent.send(text):
                await self._handle_event(event)
        except asyncio.CancelledError:
            self._finish_stream(interrupted=True)
            self._set_status("已取消")
            raise
        except Exception as exc:
            self._finish_stream(interrupted=True)
            await self._append(ErrorMessage(f"界面处理请求时出错：{exc}"))
            self._set_status("本轮失败")
        finally:
            self._end_turn()

    def _start_compact(self) -> None:
        if self._turn_active:
            self.notify("请先等待本轮结束，或按 Esc 取消。", severity="warning")
            return
        self._begin_turn()
        self._set_status("正在压缩上下文...")
        self._turn_worker = self.run_worker(
            self._compact_worker(),
            name="slash-compact",
            group="slash",
            exclusive=True,
        )

    async def _compact_worker(self) -> None:
        try:
            result = await self.agent.compact()
            self.push_screen(
                CompactionPanel(result),
                callback=self._on_compaction_panel_closed,
            )
            await self._append(
                NoticeMessage(
                    f"上下文已压缩：{result.before_messages} 条消息 -> "
                    f"{result.after_messages} 条，"
                    f"{compact_tokens(result.before_tokens)} -> "
                    f"{compact_tokens(result.after_tokens)} tok。\n"
                    f"原始上下文已保存到归档 {result.archive_id}"
                    f"（{result.archive_path}）。\n"
                    "已弹出压缩前后对照面板（Esc 或「关闭」按钮可关闭）；"
                    "后续请求将使用压缩摘要，需要早期细节时可让 Agent 调用 "
                    "recallContext 检索归档。"
                )
            )
            self._set_status("完成")
        except asyncio.CancelledError:
            self._set_status("已取消")
            raise
        except Exception as exc:
            await self._append(ErrorMessage(f"压缩上下文失败：{exc}"))
            await self._append(
                NoticeMessage("原有上下文已保留，排查问题后可以重新执行 /compact。")
            )
            self._set_status("本轮失败")
        finally:
            self._refresh_context()
            self._end_turn()

    def _on_compaction_panel_closed(self, _result: Any = None) -> None:
        """对照面板关闭后把焦点还给输入框。"""

        self._focus_prompt()

    def _start_refine(self, draft: str) -> None:
        if self._turn_active:
            self.notify("请先等待本轮结束，或按 Esc 取消。", severity="warning")
            return
        self._begin_turn()
        self._set_status("正在润色提示词...")
        self._turn_worker = self.run_worker(
            self._refine_worker(draft),
            name="slash-refine",
            group="slash",
            exclusive=True,
        )

    async def _refine_worker(self, draft: str) -> None:
        try:
            refined = await self.agent.refine_prompt(draft)
            try:
                self.query_one("#prompt", Input).value = refined
            except NoMatches:
                pass
            await self._append(
                NoticeMessage(
                    "提示词已润色并填入输入框，可编辑后按 Enter 发送。\n\n"
                    + refined
                )
            )
            self._set_status("完成")
        except asyncio.CancelledError:
            self._set_status("已取消")
            raise
        except Exception as exc:
            try:
                self.query_one("#prompt", Input).value = draft
            except NoMatches:
                pass
            await self._append(ErrorMessage(f"润色提示词失败：{exc}"))
            self._set_status("本轮失败")
        finally:
            self._end_turn()

    def _begin_turn(self) -> None:
        self._hide_slash_menu()
        self._turn_active = True
        self._turn_had_text = False
        self._streaming = None
        self._phase_text = ""
        self._tool_cards.clear()
        try:
            prompt = self.query_one("#prompt", Input)
            prompt.disabled = True
        except Exception:
            pass
        self._set_status("正在思考...")

    def _end_turn(self) -> None:
        self._turn_active = False
        self._turn_worker = None
        try:
            prompt = self.query_one("#prompt", Input)
            prompt.disabled = False
            # 压缩对照面板打开时先不抢焦点，关闭面板的回调会重新聚焦。
            if not isinstance(self.screen, CompactionPanel):
                prompt.focus()
        except Exception:
            pass
        if self._state not in {"已取消", "本轮失败", "上下文接近上限"}:
            self._set_status("就绪")
        self._refresh_context()

    async def _handle_event(self, event: AgentEvent) -> None:
        if isinstance(event, ContextStatsUpdated):
            self._refresh_context()

        elif isinstance(event, ContextWarning):
            await self._append(NoticeMessage(event.message))
            self.notify(
                f"上下文已达窗口的 {event.usage.warning_ratio:.0%}，"
                "请执行 /compact。",
                severity="warning",
            )
            self._set_status("上下文接近上限")
            self._refresh_context()

        elif isinstance(event, TurnStarted):
            self._set_status("正在思考...")

        elif isinstance(event, TextDelta):
            self._turn_had_text = True
            if self._streaming is None:
                self._streaming = AssistantMessage()
                await self._append(self._streaming)
                self._phase_text = ""
            self._phase_text += event.text
            now = time.monotonic()
            if now - self._last_paint >= 0.05:
                self._streaming.set_stream_body(self._phase_text)
                self._last_paint = now
                self._scroll_end()
            self._set_status("正在生成...")

        elif isinstance(event, ToolStarted):
            self._finish_stream()
            card = ToolActivity(event.name, event.arguments)
            self._tool_cards[event.call_id] = card
            await self._append(card)
            self._set_status(f"调用工具：{event.name}")

        elif isinstance(event, ToolFinished):
            card = self._tool_cards.pop(event.call_id, None)
            if card is not None:
                card.complete(event.ok, event.content, event.elapsed_ms)
            self._set_status("工具完成，继续思考...")

        elif isinstance(event, ToolRoundLimitReached):
            await self._append(NoticeMessage(event.message))

        elif isinstance(event, TurnCompleted):
            self._finish_stream()
            if event.content and not self._turn_had_text and not event.limit_reached:
                message = AssistantMessage()
                message.set_stream_body(event.content)
                message.finish()
                await self._append(message)
            elif not event.content and not self._turn_had_text and not event.limit_reached:
                await self._append(NoticeMessage("模型没有返回文本内容。"))
            self._set_status("完成")

        elif isinstance(event, TurnFailed):
            self._finish_stream(interrupted=True)
            await self._append(ErrorMessage(event.message))
            self._set_status("本轮失败")

    def _finish_stream(self, interrupted: bool = False) -> None:
        if self._streaming is None:
            return
        self._streaming.set_stream_body(self._phase_text)
        self._streaming.finish(interrupted=interrupted)
        self._streaming = None
        self._phase_text = ""

    # -- UI 操作 ----------------------------------------------------------

    async def _append(self, widget: Any) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(widget)
        self._scroll_end()

    def _scroll_end(self) -> None:
        try:
            self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    def _set_status(self, state: str) -> None:
        self._state = state
        try:
            self.query_one("#status", Static).update(
                status_text(self.config.llm.model, state)
            )
        except Exception:
            pass

    async def action_clear_chat(self) -> None:
        if self._turn_active:
            self.notify("请先等待本轮结束，或按 Esc 取消。", severity="warning")
            return
        try:
            self.agent.clear()
        except XAgentError as exc:
            self.notify(str(exc), severity="error")
            return

        self._finish_stream()
        self._tool_cards.clear()
        self._hide_slash_menu()
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.remove_children()
        self._set_status("就绪")
        self._refresh_context()

    async def _reset_transcript(self) -> None:
        """清空会话区，重新挂载系统提示词卡片。"""

        self._finish_stream()
        self._tool_cards.clear()
        self._hide_slash_menu()
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.remove_children()
        await self._mount_system_prompt(collapsed=True)

    async def _append_agents_notice(self) -> None:
        document = self.agent.agents_md
        if document is None:
            return
        await self._append(
            NoticeMessage(
                f"已注入项目指令 {document.name}：{document.path}"
                + ("（内容已截断）" if document.truncated else "")
            )
        )

    async def _render_history(self) -> None:
        """把当前（或刚恢复的）会话历史重新画到会话区。"""

        await self._reset_transcript()
        pending_calls: dict[str, tuple[str, str]] = {}
        for message in list(self.agent.messages)[1:]:
            if message.role == "system":
                continue
            if is_agents_md_message(message):
                await self._append_agents_notice()
                continue
            if message.role == "user":
                await self._append(UserMessage(message.content or ""))
                continue
            if message.role == "assistant":
                for call in message.tool_calls:
                    pending_calls[call.id] = (call.name, call.arguments)
                if message.content:
                    widget = AssistantMessage()
                    widget.set_stream_body(message.content)
                    widget.finish()
                    await self._append(widget)
                continue
            if message.role == "tool":
                name, arguments = pending_calls.pop(
                    message.tool_call_id or "",
                    ("tool", "{}"),
                )
                card = ToolActivity(name, arguments)
                card.complete(True, message.content or "", 0)
                await self._append(card)

    def action_cancel_turn(self) -> None:
        if self._slash_menu_visible():
            self._hide_slash_menu()
            return
        if not self._turn_active:
            return
        self._set_status("正在取消...")
        if self._turn_worker is not None:
            self._turn_worker.cancel()
            self.notify("已请求取消本轮生成。", severity="warning")

    async def action_quit(self) -> None:
        if self._turn_active and self._turn_worker is not None:
            self._turn_worker.cancel()
        if self.config.sessions.save_on_exit:
            try:
                summary = self.agent.save_session()
                if summary is not None:
                    self.notify(
                        f"会话已自动保存：{summary.session_id}",
                        severity="information",
                    )
            except Exception:
                # 退出阶段保存失败不阻塞退出。
                pass
        self.exit()
