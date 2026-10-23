"""TUI 组件：只负责渲染，不调用模型或文件系统。"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Collapsible, Markdown, Static

from .formatting import pretty_arguments, pretty_output, sanitize_text


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
