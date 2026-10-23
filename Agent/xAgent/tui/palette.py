"""中文命令面板。

Textual 自带的命令面板包含 “Search for commands…”“No matches found”
等英文文案。这里通过子类替换输入提示和空结果提示，命令本身则由
FileAgentApp.get_system_commands() 全部以中文提供。
"""

from __future__ import annotations

from rich.align import Align
from rich.text import Text
from textual.command import CommandList, CommandPalette
from textual.widgets.option_list import Option


class ChineseCommandPalette(CommandPalette):
    """中文界面的命令面板。"""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("placeholder", "搜索命令...")
        super().__init__(*args, **kwargs)

    def _start_no_matches_countdown(self, search_value: str) -> None:
        """把内置的 “No matches found” 替换为中文提示。"""
        self._stop_no_matches_countdown()

        def show_no_matches() -> None:
            if search_value:
                self.query_one(CommandList).add_option(
                    Option(
                        Align.center(
                            Text("没有找到匹配的命令", style="not bold")
                        ),
                        disabled=True,
                        id=self._NO_MATCHES,
                    )
                )
                self._list_visible = True
            else:
                self._list_visible = False

        self._no_matches_timer = self.set_timer(
            self._NO_MATCHES_COUNTDOWN,
            show_no_matches,
        )
