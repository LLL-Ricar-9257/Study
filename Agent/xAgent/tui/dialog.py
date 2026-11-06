"""启动阶段的简单对话框。

只用于配置刚生成、缺少依赖等“还没进入主界面”的场景。Textual 不可用或
不在交互终端时回退为普通文本输出，保证远程/管道场景也能看到提示。
"""

from __future__ import annotations

import sys


def show_notice_dialog(title: str, body: str, *, button: str = "退出") -> bool:
    """弹出一个居中对话框；成功显示返回 ``True``。"""

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # 管道、CI 或重定向输出时没有可交互终端，改用普通文本。
        print(f"{title}\n\n{body}", file=sys.stderr)
        return False
    try:
        return _run_dialog(title, body, button)
    except Exception:
        print(f"{title}\n\n{body}", file=sys.stderr)
        return False


def _run_dialog(title: str, body: str, button: str) -> bool:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.widgets import Button, Static

    class _NoticeApp(App[None]):
        CSS = """
        Screen { background: #120f18; color: #f6eef8; align: center middle; }
        #notice-dialog {
            width: 80;
            max-width: 92%;
            height: auto;
            padding: 1 2;
            background: #17121f;
            border: none;
        }
        #notice-title { color: #ffd4b8; text-style: bold; height: auto; }
        #notice-body { color: #f6eef8; height: auto; margin-top: 1; }
        #notice-hint { color: #776b83; height: auto; margin-top: 1; }
        #notice-actions { height: 3; margin-top: 1; align-vertical: middle; }
        #notice-close { width: 14; }
        """

        BINDINGS = [Binding("escape", "close", "关闭", show=False)]

        def compose(self) -> ComposeResult:
            with Vertical(id="notice-dialog"):
                yield Static(title, id="notice-title", markup=False)
                yield Static(body, id="notice-body", markup=False)
                yield Static("按 Enter 或 Esc 关闭", id="notice-hint", markup=False)
                with Vertical(id="notice-actions"):
                    yield Button(button, id="notice-close", variant="primary")

        def on_mount(self) -> None:
            self.query_one("#notice-close", Button).focus()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            event.stop()
            self.exit()

        def action_close(self) -> None:
            self.exit()

    _NoticeApp().run()
    return True


__all__ = ["show_notice_dialog"]
