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

from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Header, Input, Static

from core import (
    TOOL_COUNT,
    AgentEvent,
    AppConfig,
    FileAgent,
    TextDelta,
    ToolFinished,
    ToolRoundLimitReached,
    ToolStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    XAgentError,
)

from .formatting import status_text
from .palette import ChineseCommandPalette
from .widgets import (
    AssistantMessage,
    ErrorMessage,
    NoticeMessage,
    PromptInput,
    ToolActivity,
    UserMessage,
)

HELP_TEXT = """命令
  /help         显示这段帮助
  /clear        清空界面与对话历史
  /workspace    查看当前工作区
  /cancel       取消正在生成的一轮
  /quit         退出 xAgent

快捷键
  Esc           取消本轮生成
  Ctrl+L        清空对话
  Ctrl+V        粘贴系统剪贴板文本
  Ctrl+P        打开命令面板
  Ctrl+Q        退出

其它
  直接输入自然语言，Enter 发送。
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

    def __init__(self, agent: FileAgent, config: AppConfig) -> None:
        super().__init__()
        self.agent = agent
        self.config = config
        self._turn_active = False
        self._turn_had_text = False
        self._turn_worker: Any = None
        self._streaming: AssistantMessage | None = None
        self._phase_text = ""
        self._last_paint = 0.0
        self._tool_cards: dict[str, ToolActivity] = {}
        self._state = "就绪"
        self._agent_closed = False

    # -- 布局 -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="shell"):
            with Vertical(id="sidebar"):
                yield Static("xAgent", classes="brand")
                yield Static("LOCAL FILE AGENT", classes="tagline")
                yield Static(
                    "模型\n" + self.config.llm.model,
                    id="sidebar-model",
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
                    f"内置工具\n{TOOL_COUNT} 项能力\n文件 / 日期时间",
                    classes="side-block",
                    markup=False,
                )
                yield Static(
                    "快捷键\nEsc  取消生成\nCtrl+V  粘贴\nCtrl+L  清空\nCtrl+P  命令\nCtrl+Q  退出",
                    classes="side-block",
                    markup=False,
                )
            with Vertical(id="main"):
                yield VerticalScroll(id="transcript")
                with Horizontal(id="composer"):
                    yield Static(">", id="prompt-prefix", markup=False)
                    yield PromptInput(
                        placeholder=" 向 xAgent 描述任务，Enter 发送...",
                        id="prompt",
                        compact=True,
                    )
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

    async def on_unmount(self) -> None:
        if self._agent_closed:
            return
        try:
            await self.agent.aclose()
            self._agent_closed = True
        except Exception:
            # 退出阶段不阻塞 UI，也不向用户抛错。
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

    def _focus_prompt(self) -> None:
        self.query_one("#prompt", Input).focus()

    def _refresh_sidebar(self) -> None:
        try:
            self.query_one("#sidebar-model", Static).update(
                "模型\n" + self.config.llm.model
            )
            self.query_one("#sidebar-workspace", Static).update(
                "工作区\n" + str(self.config.workspace)
            )
        except Exception:
            pass

    # -- 输入与命令 -------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        if text.lower() in {"exit", "quit", "退出"}:
            self.exit()
            return

        if text.startswith("/"):
            await self._handle_command(text)
            return

        if self._turn_active:
            self.notify("上一轮仍在处理中，请稍候或按 Esc 取消。", severity="warning")
            return

        self._turn_worker = self.run_worker(
            self._run_turn(text),
            name="agent-turn",
            group="agent",
            exclusive=True,
        )

    async def _handle_command(self, text: str) -> None:
        command, _, _argument = text.partition(" ")
        command = command.lower()
        if command == "/help":
            await self._append(NoticeMessage(HELP_TEXT))
        elif command == "/clear":
            await self.action_clear_chat()
        elif command in {"/quit", "/exit"}:
            self.exit()
        elif command == "/workspace":
            await self._append(
                NoticeMessage(f"当前工作区：{self.config.workspace}")
            )
        elif command == "/cancel":
            self.action_cancel_turn()
        else:
            await self._append(
                NoticeMessage(f"未知命令：{command}。输入 /help 查看可用命令。")
            )

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

    def _begin_turn(self) -> None:
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
            prompt.focus()
        except Exception:
            pass
        if self._state not in {"已取消", "本轮失败"}:
            self._set_status("就绪")

    async def _handle_event(self, event: AgentEvent) -> None:
        if isinstance(event, TurnStarted):
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
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.remove_children()
        self._set_status("就绪")

    def action_cancel_turn(self) -> None:
        if not self._turn_active:
            return
        self._set_status("正在取消...")
        if self._turn_worker is not None:
            self._turn_worker.cancel()
            self.notify("已请求取消本轮生成。", severity="warning")

    async def action_quit(self) -> None:
        if self._turn_active and self._turn_worker is not None:
            self._turn_worker.cancel()
        self.exit()
