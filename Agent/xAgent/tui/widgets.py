"""TUI 组件：只负责渲染，不调用模型或文件系统。"""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from core import CompactionResult, ContextUsage, SessionSummary

from .clipboard import read_clipboard_text
from .formatting import (
    context_bar_text,
    pretty_arguments,
    pretty_output,
    sanitize_text,
)
from .slash import SlashCommand


def _flatten_paste(text: str) -> str:
    """把多行剪贴板内容压成单行，适配单行 composer。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = (line.strip() for line in normalized.split("\n"))
    return " ".join(line for line in lines if line)


class PromptInput(Input):
    """支持直接读取系统剪贴板的输入框。"""

    BINDINGS = [
        Binding("up", "slash_previous", "上一条命令", show=False),
        Binding("down", "slash_next", "下一条命令", show=False),
        Binding("ctrl+shift+v", "paste", "粘贴", show=False),
        Binding("shift+insert", "paste", "粘贴", show=False),
        *Input.BINDINGS,
    ]

    def action_slash_previous(self) -> None:
        move = getattr(self.app, "slash_move", None)
        if callable(move):
            move(-1)

    def action_slash_next(self) -> None:
        move = getattr(self.app, "slash_move", None)
        if callable(move):
            move(1)

    def action_paste(self) -> None:
        text = read_clipboard_text() or self.app.clipboard
        if text:
            self._insert_paste(text)

    def _on_paste(self, event: events.Paste) -> None:
        if event.text:
            self._insert_paste(event.text)
        event.stop()

    def _insert_paste(self, text: str) -> None:
        flattened = _flatten_paste(text)
        if not flattened:
            return
        start, end = self.selection
        self.replace(flattened, start, end)


class UserMessage(Static):
    def __init__(self, content: str) -> None:
        text = Text()
        text.append("你\n", style="bold #b8e6d0")
        text.append(sanitize_text(content), style="#f6eef8")
        super().__init__(text, classes="message user", markup=False)


class AssistantMessage(Markdown):
    """可增量更新的 Markdown 消息。"""

    def __init__(self) -> None:
        super().__init__("|", classes="message assistant")
        self._body = ""

    def set_stream_body(self, body: str) -> None:
        self._body = sanitize_text(body)
        self.update(f"{self._body}\n\n|" if self._body else "|")

    def finish(self, interrupted: bool = False) -> None:
        if interrupted:
            body = f"{self._body}\n\n*[本轮已中断]*" if self._body else "*[本轮已中断]*"
        else:
            body = self._body or "*（模型未返回文本内容）*"
        self._body = body
        self.update(body)


class SystemPromptCard(Collapsible):
    """运行时展示的系统提示词，可点击标题或按 Enter 展开/收起。"""

    def __init__(self, prompt: str, *, collapsed: bool = False) -> None:
        self._prompt = prompt or ""
        content = sanitize_text(self._prompt) or "（系统提示词为空）"
        self._body_widget = Static(
            Text(content, style="#cdb4db"),
            markup=False,
            classes="system-prompt-body",
        )
        super().__init__(
            self._body_widget,
            title=Text(
                "系统提示词 | 点击展开/收起",
                style="bold #a8dadc",
            ),
            collapsed=collapsed,
            collapsed_symbol=">",
            expanded_symbol="v",
            classes="system-prompt",
        )

    @property
    def prompt(self) -> str:
        """原始系统提示词（未经展示层字符替换）。"""
        return self._prompt


class ToolActivity(Collapsible):
    """工具调用卡片：默认折叠，只占一行；点击标题可展开参数和结果。"""

    def __init__(self, name: str, arguments: str) -> None:
        self._tool_name = name
        self._arguments_text = arguments
        self._body_widget = Static(
            self._render_body("执行中...", arguments, None),
            markup=False,
        )
        super().__init__(
            self._body_widget,
            title=self._build_title("执行中"),
            collapsed=True,
            collapsed_symbol=">",
            expanded_symbol="v",
            classes="tool",
        )

    def complete(self, ok: bool, content: str, elapsed_ms: int) -> None:
        state = f"完成 | {elapsed_ms} ms" if ok else f"失败 | {elapsed_ms} ms"
        self._body_widget.update(
            self._render_body(
                state,
                self._arguments_text,
                content,
                ok=ok,
            )
        )
        self.title = self._build_title(state, ok=ok)
        if not ok:
            self.add_class("tool-error")
        # 工具调用默认折叠，避免多轮工具调用占满会话区；需要细节时点击展开。
        self.collapsed = True

    def _build_title(self, state: str, *, ok: bool = True) -> Text:
        return Text(
            f"tool: {self._tool_name} | {state}",
            style="bold #ffeaa7" if ok else "bold #ffb3c1",
        )

    @staticmethod
    def _render_body(
        state: str,
        arguments: str,
        output: str | None,
        *,
        ok: bool = True,
    ) -> Text:
        text = Text()
        text.append("参数\n", style="bold #a79ab3")
        text.append(pretty_arguments(arguments), style="#cdb4db")
        text.append("\n\n状态\n", style="bold #a79ab3")
        text.append(state, style="#b8e6d0" if ok else "#ffb3c1")
        if output is not None:
            text.append("\n\n结果\n", style="bold #a79ab3")
            text.append(pretty_output(output), style="#f6eef8")
        return text


class ContextBar(Static):
    """对话框下方的上下文与缓存用量状态栏。"""

    def __init__(self) -> None:
        self._usage_text = Text()
        super().__init__(
            self._usage_text,
            id="context-bar",
            classes="context-bar",
            markup=False,
        )

    @property
    def text(self) -> Text:
        """当前展示的富文本，方便测试和调试。"""

        return self._usage_text

    def show(self, usage: ContextUsage, width: int | None = None) -> None:
        self._usage_text = context_bar_text(usage, width)
        self.update(self._usage_text)


class SlashMenu(Vertical):
    """输入单个 ``/`` 时弹出的命令列表。

    焦点始终留在输入框：上下键由 App 转发到这里的 OptionList，回车由
    ``FileAgentApp.on_input_submitted`` 读取当前高亮项。
    """

    def __init__(self) -> None:
        super().__init__(id="slash-menu", classes="slash-menu")
        self.display = False

    def compose(self):
        yield Static(
            "命令   ↑↓ 选择   Enter 确认   Esc 关闭",
            classes="slash-title",
            markup=False,
        )
        yield OptionList(
            id="slash-options",
            compact=True,
            markup=False,
        )

    async def on_mount(self) -> None:
        self.display = False

    def show_commands(self, commands: tuple[SlashCommand, ...]) -> None:
        option_list = self.query_one("#slash-options", OptionList)
        if commands:
            options = [
                Option(self._command_prompt(command), id=command.name)
                for command in commands
            ]
            option_list.set_options(options)
            option_list.highlighted = 0
        else:
            option_list.set_options(
                [Option(Text("没有匹配的命令", style="#776b83"), disabled=True)]
            )
        self.display = True

    def hide(self) -> None:
        self.display = False

    @property
    def is_open(self) -> bool:
        return bool(self.display)

    def highlighted_name(self) -> str | None:
        option = self.query_one("#slash-options", OptionList).highlighted_option
        if option is None or option.disabled:
            return None
        return option.id

    def move(self, delta: int) -> None:
        option_list = self.query_one("#slash-options", OptionList)
        if delta < 0:
            option_list.action_cursor_up()
        else:
            option_list.action_cursor_down()

    @staticmethod
    def _command_prompt(command: SlashCommand) -> Text:
        text = Text()
        text.append(f"/{command.name}", style="bold #ffd4b8")
        text.append(f"  {command.title}", style="bold #b8e6d0")
        text.append(f"  {command.description}", style="#a79ab3")
        return text


class SessionPanel(ModalScreen[str | None]):
    """历史会话选择面板：↑↓ 移动、Enter 恢复、Esc 取消。"""

    BINDINGS = [Binding("escape", "cancel", "取消", show=False)]

    def __init__(self, sessions: tuple[SessionSummary, ...]) -> None:
        super().__init__()
        self.sessions = sessions

    def compose(self):
        with Vertical(id="session-dialog"):
            yield Static(
                Text(
                    f"历史会话（{len(self.sessions)}）\n"
                    "↑↓ 选择   Enter 恢复   Esc 取消",
                    style="bold #b8e6d0",
                ),
                id="session-title",
                markup=False,
            )
            yield OptionList(
                *[
                    Option(self._option_text(session), id=session.session_id)
                    for session in self.sessions
                ]
                or [Option(Text("还没有已保存的会话", style="#776b83"), disabled=True)],
                id="session-options",
                markup=False,
            )
            with Horizontal(id="session-actions"):
                yield Static(
                    "会话保存在 .agents/sessions/",
                    id="session-hint",
                    markup=False,
                )
                yield Button("取消", id="session-cancel")

    async def on_mount(self) -> None:
        self.query_one("#session-options", OptionList).focus()

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if event.option_list.id != "session-options":
            return
        event.stop()
        if event.option.id:
            self.dismiss(event.option.id)
        else:
            self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "session-cancel":
            event.stop()
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @staticmethod
    def _option_text(session: SessionSummary) -> Text:
        text = Text()
        text.append(f"{session.session_id}  ", style="#776b83")
        text.append(session.title or "新会话", style="bold #ffd4b8")
        text.append(
            f"  {session.message_count} 条 | {session.updated_at}",
            style="#a79ab3",
        )
        return text


class ErrorMessage(Static):
    def __init__(self, message: str) -> None:
        text = Text()
        text.append("错误\n", style="bold #ffb3c1")
        text.append(sanitize_text(message), style="#ffdae0")
        super().__init__(text, classes="message error", markup=False)


class NoticeMessage(Static):
    def __init__(self, message: str) -> None:
        text = Text()
        text.append(sanitize_text(message), style="#ffeaa7")
        super().__init__(text, classes="message notice", markup=False)


class CompactionPanel(ModalScreen[None]):
    """压缩前后对照的浮动面板：左栏原文、右栏摘要，均可滚动。

    面板只读，不触发任何业务动作；Esc 或「关闭」按钮退出。
    """

    BINDINGS = [Binding("escape", "close_panel", "关闭", show=False)]

    def __init__(self, result: CompactionResult) -> None:
        super().__init__()
        self.result = result

    def compose(self):
        result = self.result
        title = Text()
        title.append("上下文压缩完成", style="bold #b8e6d0")
        title.append(f"   归档 {result.archive_id or '未记录'}", style="#a79ab3")
        title.append(
            f"\n压缩前 {result.before_messages} 条 / "
            f"{result.before_tokens:,} tok"
            "   ->   "
            f"压缩后 {result.after_messages} 条 / {result.after_tokens:,} tok",
            style="#cdb4db",
        )
        with Vertical(id="compact-dialog"):
            yield Static(title, id="compact-title", markup=False)
            with Horizontal(id="compact-columns"):
                with Vertical(classes="compact-pane"):
                    yield Static(
                        "压缩前（原始上下文）",
                        classes="compact-pane-title",
                        markup=False,
                    )
                    with VerticalScroll(classes="compact-scroll"):
                        yield Static(
                            sanitize_text(result.before_text or "（没有可展示的原文）"),
                            classes="compact-body",
                            markup=False,
                        )
                with Vertical(classes="compact-pane"):
                    yield Static(
                        "压缩后（摘要 + 归档说明）",
                        classes="compact-pane-title",
                        markup=False,
                    )
                    with VerticalScroll(classes="compact-scroll"):
                        yield Static(
                            sanitize_text(result.after_text or "（没有可展示的摘要）"),
                            classes="compact-body",
                            markup=False,
                        )
            with Horizontal(id="compact-actions"):
                yield Static(
                    "Esc 关闭   |   完整原文已保存到归档文件",
                    id="compact-hint",
                    markup=False,
                )
                yield Button("关闭", id="compact-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "compact-close":
            event.stop()
            self.action_close_panel()

    def action_close_panel(self) -> None:
        if self.is_current:
            self.dismiss(None)
