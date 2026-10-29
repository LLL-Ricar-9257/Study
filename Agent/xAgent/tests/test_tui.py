from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from core import AgentLimits, AppConfig, LLMSettings
from core.models import ChatChunk, ChatMessage
from core.tools.filesystem import FileToolset
from core.tools.registry import ToolRegistry
from core.tools.specs import FILE_TOOL_SPECS
from tui.formatting import sanitize_text


class OneShotModel:
    def __init__(self, text: str = "你好，我是 xAgent。") -> None:
        self.text = text
        self.closed = False

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatChunk]:
        for char in self.text:
            yield ChatChunk(text=char)

    async def aclose(self) -> None:
        self.closed = True


class BlockingModel(OneShotModel):
    def __init__(self) -> None:
        super().__init__("开始")

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatChunk]:
        yield ChatChunk(text="开始")
        await asyncio.Event().wait()  # 直到 worker 被取消


def _make_app(tmp_path: Path, model: Any) -> Any:
    from core import FileAgent
    from tui.app import FileAgentApp

    toolset = FileToolset(tmp_path, limits=AgentLimits())
    agent = FileAgent(
        model=model,
        tools=ToolRegistry(toolset.registry_handlers(), FILE_TOOL_SPECS),
        system_prompt="test",
    )
    config = AppConfig(
        workspace=tmp_path,
        llm=LLMSettings(api_key="test", model="test-model"),
    )
    return FileAgentApp(agent=agent, config=config), agent


def test_tui_mounts_and_streams_reply(tmp_path: Path) -> None:
    from tui.widgets import AssistantMessage, UserMessage

    model = OneShotModel()
    app, _agent = _make_app(tmp_path, model)

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one("#prompt")
            assert prompt.compact
            assert app.query_one("#composer")
            assert app.query_one("#hint")
            prompt.value = "你好"
            await prompt.action_submit()
            for _ in range(40):
                await pilot.pause(0.05)
                if not app._turn_active:
                    break
            assert app.query_one(UserMessage)
            assert app.query_one(AssistantMessage)
            assert "xAgent" in app.query_one(AssistantMessage)._body

    asyncio.run(scenario())
    assert model.closed


def test_tui_cancel_rolls_back_turn(tmp_path: Path) -> None:
    model = BlockingModel()
    app, agent = _make_app(tmp_path, model)

    async def scenario() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt")
            prompt.value = "开始"
            await prompt.action_submit()
            for _ in range(40):
                await pilot.pause(0.05)
                if app._streaming is not None:
                    break
            assert app._streaming is not None

            app.action_cancel_turn()
            for _ in range(60):
                await pilot.pause(0.05)
                if not app._turn_active:
                    break
            assert not app._turn_active
            assert app._state == "已取消"
            assert len(agent.messages) == 1
            assert not app.query_one("#prompt").disabled

    asyncio.run(scenario())


def test_ambiguous_punctuation_is_normalized() -> None:
    assert sanitize_text("“你好”—……") == '"你好"-......'
    assert sanitize_text("文件 · 日期") == "文件 | 日期"
    assert sanitize_text("│─┌┐") == "|-++"


def test_tui_styles_do_not_depend_on_box_drawing() -> None:
    from pathlib import Path

    css = (
        Path(__file__).resolve().parent.parent / "tui" / "styles.tcss"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "border: round",
        "border: solid",
        "border: thick",
        "border-left: thick",
        "border-right: solid",
    ):
        assert forbidden not in css


def test_console_font_setup_can_be_disabled(monkeypatch: Any) -> None:
    from tui.terminal import configure_console_font

    monkeypatch.setenv("XAGENT_NO_FONT_SETUP", "1")
    assert configure_console_font("黑体") is False


def test_tool_card_keeps_tool_name() -> None:
    from textual.app import App

    from tui.widgets import ToolActivity

    class Host(App[None]):
        def __init__(self) -> None:
            super().__init__()
            self.card = ToolActivity("list_directory", '{"path": "."}')

        def compose(self):
            yield self.card

    async def scenario() -> None:
        app = Host()
        async with app.run_test():
            app.card.complete(True, "ok", 12)
            assert "list_directory" in str(app.card.title)

    asyncio.run(scenario())


def test_command_palette_is_fully_chinese(tmp_path: Path) -> None:
    from textual.command import CommandInput, CommandList

    from tui.palette import ChineseCommandPalette

    app, _agent = _make_app(tmp_path, OneShotModel())

    def has_chinese(text: str) -> bool:
        return any("\u4e00" <= char <= "\u9fff" for char in text)

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            commands = list(app.get_system_commands(app.screen))
            assert commands
            assert all(has_chinese(command.title) for command in commands)
            assert all(has_chinese(command.help) for command in commands)

            await pilot.press("ctrl+p")
            await pilot.pause(0.2)
            palette = app.screen
            assert isinstance(palette, ChineseCommandPalette)
            assert palette.query_one(CommandInput).placeholder == "搜索命令..."

            command_input = palette.query_one(CommandInput)
            command_input.value = "不存在的命令zzz"
            await pilot.pause(0.8)
            options = palette.query_one(CommandList).options
            assert any("没有找到匹配的命令" in str(option.prompt) for option in options)

    asyncio.run(scenario())


def test_prompt_pastes_multiline_system_clipboard(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from tui.widgets import PromptInput

    monkeypatch.setattr(
        "tui.widgets.read_clipboard_text",
        lambda: "第一行内容\n第二行内容",
    )
    app, _agent = _make_app(tmp_path, OneShotModel())

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.focus()
            await pilot.press("ctrl+v")
            await pilot.pause()
            assert prompt.value == "第一行内容 第二行内容"

    asyncio.run(scenario())


def test_transcript_reflows_after_blur_and_resize(tmp_path: Path) -> None:
    from textual import events
    from textual.containers import VerticalScroll

    from tui.widgets import UserMessage

    app, _agent = _make_app(tmp_path, OneShotModel())

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            for index in range(20):
                await app._append(UserMessage(f"消息 {index}"))
            await pilot.pause()

            transcript = app.query_one("#transcript", VerticalScroll)
            assert transcript.is_vertical_scroll_end

            app.post_message(events.AppBlur())
            await pilot.pause()
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            app.post_message(events.AppFocus())
            await pilot.pause(0.2)

            assert app.screen.size == (80, 24)
            assert transcript.is_vertical_scroll_end

    asyncio.run(scenario())

