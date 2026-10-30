"""TUI 组件：只负责渲染，不调用模型或文件系统。"""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.widgets import Collapsible, Input, Markdown, Static

from .clipboard import read_clipboard_text
from .formatting import pretty_arguments, pretty_output, sanitize_text


def _flatten_paste(text: str) -> str:
    """把多行剪贴板内容压成单行，适配单行 composer。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = (line.strip() for line in normalized.split("\n"))
    return " ".join(line for line in lines if line)


class PromptInput(Input):
    """支持直接读取系统剪贴板的输入框。"""

    BINDINGS = [
        Binding("ctrl+shift+v", "paste", "粘贴", show=False),
        Binding("shift+insert", "paste", "粘贴", show=False),
        *Input.BINDINGS,
    ]

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
    """工具调用卡片。"""

    def __init__(self, name: str, arguments: str) -> None:
        self._tool_name = name
        self._arguments_text = arguments
        self._body_widget = Static(
            self._render_body("执行中...", arguments, None),
            markup=False,
        )
        super().__init__(
            self._body_widget,
            title=Text(f"tool: {name}", style="bold #ffeaa7"),
            collapsed=False,
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
        self.title = Text(
            f"tool: {self._tool_name}",
            style="bold #ffeaa7" if ok else "bold #ffb3c1",
        )
        if not ok:
            self.add_class("tool-error")
        self.collapsed = ok

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
