from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from core import AgentLimits, AppConfig, ContextUsage, LLMSettings
from core.models import ChatChunk, ChatMessage, TokenUsage
from core.tools.filesystem import FileToolset
from core.tools.registry import ToolRegistry
from core.tools.specs import FILE_TOOL_SPECS
from tui.formatting import context_bar_text, sanitize_text


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


class UsageModel(OneShotModel):
    """在正文之后返回一次带缓存信息的 usage 分片。"""

    def __init__(self) -> None:
        super().__init__("缓存命中测试")

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatChunk]:
        yield ChatChunk(text=self.text)
        yield ChatChunk(
            usage=TokenUsage(
                prompt_tokens=1_000,
                completion_tokens=50,
                total_tokens=1_050,
                cached_tokens=600,
                cache_miss_tokens=400,
            )
        )


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

def _make_app(
    tmp_path: Path,
    model: Any,
    model_metadata: Any = None,
) -> Any:
    from core import ContextArchive, ContextTracker, FileAgent, SessionStore
    from tui.app import FileAgentApp

    toolset = FileToolset(tmp_path, limits=AgentLimits())
    agent = FileAgent(
        model=model,
        tools=ToolRegistry(toolset.registry_handlers(), FILE_TOOL_SPECS),
        system_prompt="test",
        context=(
            ContextTracker(model_metadata.context_window or 0)
            if model_metadata is not None
            else None
        ),
        archive=ContextArchive(tmp_path),
        session_store=SessionStore(tmp_path),
        workspace=tmp_path,
    )
    config = AppConfig(
        workspace=tmp_path,
        llm=LLMSettings(api_key="test", model="test-model"),
    )
    return (
        FileAgentApp(agent=agent, config=config, model_metadata=model_metadata),
        agent,
    )


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


def test_context_bar_shows_cache_usage_below_prompt(tmp_path: Path) -> None:
    from tui.widgets import ContextBar, NoticeMessage

    app, _agent = _make_app(tmp_path, UsageModel())

    async def scenario() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            bar = app.query_one(ContextBar)
            assert "上下文" in bar.text.plain

            prompt = app.query_one("#prompt")
            prompt.value = "你好"
            await prompt.action_submit()
            for _ in range(60):
                await pilot.pause(0.05)
                if not app._turn_active:
                    break
            await pilot.pause()

            plain = bar.text.plain
            assert "缓存" in plain
            assert "命中 600" in plain
            assert "未命中 400" in plain
            assert "60%" in plain
            assert "消息 3" in plain

            prompt.value = "/context"
            await prompt.action_submit()
            await pilot.pause()
            notices = [
                notice.render().plain for notice in app.query(NoticeMessage)
            ]
            assert any("上下文用量" in notice for notice in notices)
            assert any("命中率 60.0%" in notice for notice in notices)

            await app.action_clear_chat()
            await pilot.pause()
            assert "消息 1" in bar.text.plain

    asyncio.run(scenario())


def test_upstream_model_context_is_displayed(tmp_path: Path) -> None:
    from core import ModelMetadata
    from tui.widgets import ContextBar, NoticeMessage

    metadata = ModelMetadata(
        model="test-model",
        context_window=1_048_576,
        max_output_tokens=393_216,
        display_name="DeepSeek-V4.1-Flash",
        endpoint="https://api.deepseek.com/v1/models",
    )
    app, _agent = _make_app(tmp_path, OneShotModel(), metadata)

    async def scenario() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            sidebar = str(app.query_one("#sidebar-context").render())
            assert "1.0M" in sidebar
            assert "上游获取" in sidebar

            bar = app.query_one(ContextBar)
            assert "1.0M" in bar.text.plain

            notices = [notice.render().plain for notice in app.query(NoticeMessage)]
            assert any("1,048,576" in notice for notice in notices)
            assert any("从上游" in notice for notice in notices)

            prompt = app.query_one("#prompt")
            prompt.value = "/model"
            await prompt.action_submit()
            await pilot.pause()
            notices = [notice.render().plain for notice in app.query(NoticeMessage)]
            assert any("1,048,576" in notice for notice in notices)
            assert any("test-model" in notice for notice in notices)

    asyncio.run(scenario())


def test_context_bar_hides_details_on_narrow_width() -> None:
    usage = ContextUsage(
        context_window=1_000,
        context_tokens=200,
        estimated=False,
        total_cached_tokens=120,
        total_cache_miss_tokens=80,
        cache_reports=1,
        messages=5,
    )
    narrow = context_bar_text(usage, 40).plain
    assert "上下文" in narrow
    assert "缓存" not in narrow
    assert "消息" not in narrow


def test_slash_menu_opens_filters_and_closes(tmp_path: Path) -> None:
    from tui.widgets import SlashMenu

    app, _agent = _make_app(tmp_path, OneShotModel())

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            menu = app.query_one(SlashMenu)
            assert menu.is_open is False

            await pilot.press("/")
            await pilot.pause()
            assert menu.is_open is True
            option_ids = [
                option.id for option in app.query_one("#slash-options").options
            ]
            assert "compact" in option_ids
            assert "refine" in option_ids
            assert menu.highlighted_name() == "new"

            await pilot.press("down")
            assert menu.highlighted_name() == "resume"
            await pilot.press("up")
            assert menu.highlighted_name() == "new"

            await pilot.press("c", "o", "m", "p")
            await pilot.pause()
            assert menu.highlighted_name() == "compact"
            assert [
                option.id for option in app.query_one("#slash-options").options
            ] == ["compact"]

            await pilot.press("escape")
            await pilot.pause()
            assert menu.is_open is False
            assert app._turn_active is False

    asyncio.run(scenario())


def test_slash_menu_enter_selects_command_not_first_match(tmp_path: Path) -> None:
    from tui.widgets import SlashMenu

    app, _agent = _make_app(tmp_path, OneShotModel())

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            menu = app.query_one(SlashMenu)
            await pilot.press("/", "c", "o", "n", "t", "e", "x", "t")
            await pilot.pause()
            assert menu.highlighted_name() == "context"

            await pilot.press("enter")
            await pilot.pause()
            assert menu.is_open is False
            # /context 会把明细追加到会话区。
            from tui.widgets import NoticeMessage

            notices = [notice.render().plain for notice in app.query(NoticeMessage)]
            assert any("上下文用量" in notice for notice in notices)

    asyncio.run(scenario())


def test_slash_template_fills_prompt_without_sending(tmp_path: Path) -> None:
    app, _agent = _make_app(tmp_path, OneShotModel())

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one("#prompt")
            await pilot.press("/", "r", "e", "v", "i", "e", "w")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert prompt.value.startswith("请审查当前工作区")
            assert app._turn_active is False

    asyncio.run(scenario())


def test_slash_compose_then_refine_updates_input(tmp_path: Path) -> None:
    app, _agent = _make_app(tmp_path, OneShotModel("这是润色后的提示词。"))

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one("#prompt")
            await pilot.press("/", "r", "e", "f", "i", "n", "e")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert prompt.value == "/refine "
            assert app._turn_active is False

            prompt.value = "/refine 看看 readme"
            await prompt.action_submit()
            for _ in range(60):
                await pilot.pause(0.05)
                if not app._turn_active:
                    break
            await pilot.pause()
            assert prompt.value == "这是润色后的提示词。"
            assert prompt.disabled is False

    asyncio.run(scenario())


def test_slash_compact_compresses_history(tmp_path: Path) -> None:
    from tui.widgets import CompactionPanel, NoticeMessage

    app, agent = _make_app(tmp_path, OneShotModel("第一轮回复"))

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one("#prompt")
            prompt.value = "第一轮问题"
            await prompt.action_submit()
            for _ in range(60):
                await pilot.pause(0.05)
                if not app._turn_active:
                    break
            assert len(agent.messages) == 3

            prompt.value = "/compact"
            await prompt.action_submit()
            for _ in range(80):
                await pilot.pause(0.05)
                if not app._turn_active:
                    break
            await pilot.pause()
            assert len(agent.messages) == 3
            summary = agent.messages[1].content or ""
            assert "压缩摘要" in summary
            assert "recallContext" in summary
            # 压缩成功后才弹出对照面板，左栏原文、右栏摘要。
            panel = app.screen
            assert isinstance(panel, CompactionPanel)
            assert "第一轮问题" in panel.result.before_text
            assert "压缩摘要" in panel.result.after_text
            # 面板是两栏布局，带一个可以关闭的按钮。
            from textual.containers import Horizontal

            columns = panel.query_one("#compact-columns", Horizontal)
            assert len(list(columns.children)) == 2
            assert len(panel.query(".compact-scroll")) == 2
            assert panel.query_one("#compact-close")
            # 归档必须先落盘，并且能按编号检索到原始内容。
            archives = list((tmp_path / ".agents" / "context").glob("*.jsonl"))
            assert len(archives) == 1
            assert archives[0].stem == panel.result.archive_id
            assert "第一轮问题" in archives[0].read_text(encoding="utf-8")

            panel.action_close_panel()
            await pilot.pause()
            assert not isinstance(app.screen, CompactionPanel)
            notices = [notice.render().plain for notice in app.query(NoticeMessage)]
            assert any("上下文已压缩" in notice for notice in notices)
            assert any(panel.result.archive_id in notice for notice in notices)

    asyncio.run(scenario())


def test_context_bar_shows_default_window_when_upstream_unknown(tmp_path: Path) -> None:
    from tui.widgets import ContextBar

    app, _agent = _make_app(tmp_path, OneShotModel())

    async def scenario() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            plain = app.query_one(ContextBar).text.plain
            assert "128k" in plain
            assert "默认" in plain
            sidebar = str(app.query_one("#sidebar-context").render())
            assert "128k" in sidebar
            assert "默认" in sidebar

    asyncio.run(scenario())


class ThresholdModel:
    """第一轮就返回接近窗口上限的用量，之后返回摘要/普通回复。"""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatChunk]:
        self.calls += 1
        if self.calls == 1:
            yield ChatChunk(text="第一轮回复")
            yield ChatChunk(
                usage=TokenUsage(
                    prompt_tokens=800,
                    completion_tokens=100,
                    total_tokens=900,
                )
            )
        elif self.calls == 2:
            yield ChatChunk(text="压缩摘要")
        else:
            yield ChatChunk(text="继续工作")

    async def aclose(self) -> None:
        return None


def test_context_threshold_blocks_prompts_until_compact(tmp_path: Path) -> None:
    from core import ModelMetadata
    from tui.widgets import NoticeMessage

    metadata = ModelMetadata(model="test-model", context_window=1_000)
    app, agent = _make_app(tmp_path, ThresholdModel(), metadata)

    async def wait_turn(pilot: Any) -> None:
        for _ in range(80):
            await pilot.pause(0.05)
            if not app._turn_active:
                break
        await pilot.pause()

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one("#prompt")
            prompt.value = "第一轮问题"
            await prompt.action_submit()
            await wait_turn(pilot)
            assert agent.context_limit_reached
            assert "compact" in prompt.placeholder
            notices = [notice.render().plain for notice in app.query(NoticeMessage)]
            assert any("80%" in notice for notice in notices)

            # 达到阈值后普通提示词被拦截，历史不再增长。
            before = len(agent.messages)
            prompt.value = "第二轮普通提问"
            await prompt.action_submit()
            await pilot.pause()
            assert len(agent.messages) == before
            notices = [notice.render().plain for notice in app.query(NoticeMessage)]
            assert any("已暂停接受新的普通提示词" in notice for notice in notices)

            # /compact 仍然被接受；压缩后解除限制并恢复输入。
            prompt.value = "/compact"
            await prompt.action_submit()
            await wait_turn(pilot)
            assert not agent.context_limit_reached
            assert "compact" not in prompt.placeholder
            assert "压缩摘要" in (agent.messages[1].content or "")

            prompt.value = "继续"
            await prompt.action_submit()
            await wait_turn(pilot)
            assert "继续工作" in (agent.messages[-1].content or "")

    asyncio.run(scenario())


def test_slash_save_new_and_resume(tmp_path: Path) -> None:
    from tui.widgets import NoticeMessage, SessionPanel, UserMessage

    app, agent = _make_app(tmp_path, OneShotModel("第一轮回复"))

    async def wait_turn(pilot: Any) -> None:
        for _ in range(80):
            await pilot.pause(0.05)
            if not app._turn_active:
                break
        await pilot.pause()

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one("#prompt")
            prompt.value = "第一轮问题"
            await prompt.action_submit()
            await wait_turn(pilot)
            first_session = agent.session_id

            prompt.value = "/save"
            await prompt.action_submit()
            await pilot.pause()
            saved = (tmp_path / ".agents" / "sessions" / f"{first_session}.jsonl")
            assert saved.is_file()
            assert "第一轮问题" in saved.read_text(encoding="utf-8")
            notices = [notice.render().plain for notice in app.query(NoticeMessage)]
            assert any("会话已保存" in notice for notice in notices)

            prompt.value = "/new"
            await prompt.action_submit()
            await pilot.pause()
            assert len(agent.messages) == 1
            assert agent.session_id != first_session

            prompt.value = "/resume"
            await prompt.action_submit()
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, SessionPanel)
            # Esc 取消：面板关闭且不恢复会话。
            await pilot.press("escape")
            for _ in range(20):
                await pilot.pause(0.05)
                if not isinstance(app.screen, SessionPanel):
                    break
            assert not isinstance(app.screen, SessionPanel)
            assert len(agent.messages) == 1

            prompt = app.query_one("#prompt")
            prompt.value = "/resume"
            await prompt.action_submit()
            await pilot.pause()
            assert isinstance(app.screen, SessionPanel)
            await pilot.press("enter")
            for _ in range(80):
                await pilot.pause(0.05)
                if not isinstance(app.screen, SessionPanel) and len(agent.messages) == 3:
                    break
            await pilot.pause()
            assert len(agent.messages) == 3
            assert agent.session_id == first_session
            assert app.query(UserMessage)
            assert app.query_one("#transcript")

    asyncio.run(scenario())


def test_slash_session_info_and_rename(tmp_path: Path) -> None:
    from tui.widgets import NoticeMessage

    app, agent = _make_app(tmp_path, OneShotModel())

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one("#prompt")
            prompt.value = "/session"
            await prompt.action_submit()
            await pilot.pause()
            notices = [notice.render().plain for notice in app.query(NoticeMessage)]
            assert any("当前会话" in notice for notice in notices)
            assert any("会话目录" in notice for notice in notices)

            prompt.value = "/rename 测试标题"
            await prompt.action_submit()
            await pilot.pause()
            assert agent.session_title == "测试标题"
            notices = [notice.render().plain for notice in app.query(NoticeMessage)]
            assert any("测试标题" in notice for notice in notices)

            prompt.value = "/mcp"
            await prompt.action_submit()
            await pilot.pause()
            notices = [notice.render().plain for notice in app.query(NoticeMessage)]
            assert any("MCP 状态" in notice for notice in notices)

    asyncio.run(scenario())


def test_sidebar_shows_mcp_area(tmp_path: Path) -> None:
    app, _agent = _make_app(tmp_path, OneShotModel())

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sidebar = str(app.query_one("#sidebar-mcp").render())
            assert "MCP" in sidebar
            assert "未配置服务器" in sidebar

    asyncio.run(scenario())


def test_sidebar_mcp_area_refreshes_after_async_load(tmp_path: Path) -> None:
    from core import ContextArchive, FileAgent, McpServerSummary, SessionStore

    class FakeMcp:
        def __init__(self) -> None:
            self.summaries = (
                McpServerSummary(
                    name="demo",
                    transport="stdio",
                    endpoint="python server.py",
                    connected=True,
                    tool_count=2,
                    tool_names=("demo__echo", "demo__calc"),
                    prefix="demo",
                ),
            )

        def start(self) -> None:
            return None

        def registry_handlers(self) -> dict[str, Any]:
            return {}

        def tool_specs(self) -> tuple[Any, ...]:
            return ()

        def notices(self) -> tuple[str, ...]:
            return ("MCP 服务器「demo」已连接。",)

        def close(self) -> None:
            return None

    from tui.app import FileAgentApp

    agent = FileAgent(
        model=OneShotModel(),
        tools=ToolRegistry(FileToolset(tmp_path).registry_handlers(), FILE_TOOL_SPECS),
        system_prompt="test",
        archive=ContextArchive(tmp_path),
        session_store=SessionStore(tmp_path),
        workspace=tmp_path,
        mcp_manager=FakeMcp(),  # type: ignore[arg-type]
    )
    config = AppConfig(
        workspace=tmp_path,
        llm=LLMSettings(api_key="test", model="test-model"),
    )
    app = FileAgentApp(agent=agent, config=config)

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(60):
                await pilot.pause(0.05)
                text = str(app.query_one("#sidebar-mcp").render())
                if "demo" in text:
                    break
            assert "demo" in text
            assert "demo__echo" in text
            assert "2 tools" in text

    asyncio.run(scenario())


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


def test_system_prompt_is_shown_and_collapsible(tmp_path: Path) -> None:
    from tui.widgets import SystemPromptCard

    app, agent = _make_app(tmp_path, OneShotModel())

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            card = app.query_one(SystemPromptCard)
            assert card.prompt == agent.system_prompt
            # 启动时默认折叠，用户点击标题或 /system 才展开。
            assert card.collapsed is True

            await app.action_toggle_system_prompt()
            await pilot.pause()
            assert card.collapsed is False

            await app.action_toggle_system_prompt()
            await pilot.pause()
            assert card.collapsed is True

            await app.action_clear_chat()
            await pilot.pause()
            assert not app.query(SystemPromptCard)

            await app.action_toggle_system_prompt()
            await pilot.pause()
            assert app.query_one(SystemPromptCard).collapsed is False

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


def test_tool_card_is_collapsed_by_default_and_after_completion() -> None:
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
            # 执行中、成功、失败都保持折叠，只通过标题展示状态。
            assert app.card.collapsed is True
            assert "执行中" in str(app.card.title)

            app.card.complete(True, "ok", 12)
            assert app.card.collapsed is True
            assert "完成" in str(app.card.title)

            app.card.complete(False, "boom", 3)
            assert app.card.collapsed is True
            assert "失败" in str(app.card.title)
            assert app.card.has_class("tool-error")

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

