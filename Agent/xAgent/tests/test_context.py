from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agent import FileAgent
from core.archive import ContextArchive
from core.config import LLMSettings
from core.context import (
    DEFAULT_CONTEXT_WINDOW,
    ContextTracker,
    estimate_messages_tokens,
    estimate_text_tokens,
)
from core.errors import ContextLimitError
from core.events import (
    ContextStatsUpdated,
    ContextWarning,
    TurnCompleted,
    TurnFailed,
)
from core.llm.openai_chat import OpenAIChatModel, _extract_usage
from core.models import ChatChunk, ChatMessage, TokenUsage, ToolCallDelta
from core.tools.context import (
    ContextArchiveToolset,
    ContextToolset,
    format_context_report,
    format_recall_report,
)
from core.tools.filesystem import FileToolset
from core.tools.registry import ToolRegistry
from core.tools.specs import CONTEXT_TOOL_SPECS, FILE_TOOL_SPECS


class ScriptedModel:
    def __init__(self, scripts: list[list[ChatChunk]]) -> None:
        self.scripts = scripts
        self.calls: list[tuple[ChatMessage, ...]] = []

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatChunk]:
        self.calls.append(tuple(messages))
        for chunk in self.scripts.pop(0):
            yield chunk

    async def aclose(self) -> None:
        return None


class FailingModel:
    async def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatChunk]:
        raise RuntimeError("boom")
        yield ChatChunk()  # pragma: no cover


def test_estimate_tokens_handles_cjk_and_ascii() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("abcd") == 1
    assert estimate_text_tokens("abcdefgh") == 2
    assert estimate_text_tokens("你好") == 2
    assert estimate_text_tokens("你好abcd") == 3


def test_tracker_accumulates_cache_usage() -> None:
    tracker = ContextTracker(context_window=1_000)
    messages = (
        ChatMessage.system("system"),
        ChatMessage.user("你好"),
        ChatMessage.assistant("world"),
    )
    usage = TokenUsage(
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        cached_tokens=60,
        cache_miss_tokens=40,
    )

    snapshot = tracker.record_request(usage, messages)
    assert snapshot.context_tokens == 120
    assert snapshot.estimated is False
    assert snapshot.prompt_tokens == 100
    assert snapshot.completion_tokens == 20
    assert snapshot.cached_tokens == 60
    assert snapshot.cache_miss_tokens == 40
    assert snapshot.total_cached_tokens == 60
    assert snapshot.total_cache_miss_tokens == 40
    assert snapshot.cache_hit_rate == pytest.approx(0.6)
    assert snapshot.cache_reports == 1
    assert snapshot.requests == 1
    assert snapshot.messages == 3
    assert snapshot.user_messages == 1
    assert snapshot.context_percent == pytest.approx(12.0)

    again = tracker.record_request(
        TokenUsage(prompt_tokens=200, completion_tokens=10, cached_tokens=50),
        messages,
    )
    assert again.total_prompt_tokens == 300
    assert again.total_completion_tokens == 30
    assert again.requests == 2
    # 第二次没有返回未命中字段，按 prompt - cached 推算为 150。
    assert again.cache_hit_rate == pytest.approx(110 / 300)


def test_tracker_without_usage_falls_back_to_estimation() -> None:
    tracker = ContextTracker(context_window=10_000)
    messages = (ChatMessage.system("system prompt"), ChatMessage.user("你好"))
    snapshot = tracker.record_request(None, messages)

    assert snapshot.estimated is True
    assert snapshot.context_tokens == estimate_messages_tokens(messages)
    assert snapshot.cache_hit_rate is None
    assert snapshot.cache_reports == 0
    assert snapshot.requests == 1


def test_tracker_reset_clears_counters() -> None:
    tracker = ContextTracker(context_window=1_000)
    messages = (ChatMessage.system("s"), ChatMessage.user("u"))
    tracker.record_request(TokenUsage(prompt_tokens=10, cached_tokens=5), messages)
    tracker.reset((ChatMessage.system("fresh"),))

    snapshot = tracker.snapshot()
    assert snapshot.requests == 0
    assert snapshot.total_prompt_tokens == 0
    assert snapshot.cache_reports == 0
    assert snapshot.messages == 1
    assert snapshot.estimated is True
    assert snapshot.context_tokens == estimate_messages_tokens(
        (ChatMessage.system("fresh"),)
    )


def test_extract_usage_supports_deepseek_and_openai_shapes() -> None:
    deepseek = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        prompt_cache_hit_tokens=64,
        prompt_cache_miss_tokens=36,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=7),
    )
    usage = _extract_usage(deepseek)
    assert usage is not None
    assert usage.cached_tokens == 64
    assert usage.cache_miss_tokens == 36
    assert usage.reasoning_tokens == 7

    openai_like = SimpleNamespace(
        prompt_tokens=50,
        completion_tokens=5,
        total_tokens=55,
        prompt_tokens_details=SimpleNamespace(cached_tokens=20),
    )
    usage = _extract_usage(openai_like)
    assert usage is not None
    assert usage.cached_tokens == 20
    assert usage.cache_miss_tokens == 30

    no_cache = _extract_usage(
        SimpleNamespace(prompt_tokens=8, completion_tokens=1, total_tokens=9)
    )
    assert no_cache is not None
    assert no_cache.cached_tokens is None
    assert _extract_usage(None) is None


def test_context_tool_reports_cache_and_message_counts() -> None:
    tracker = ContextTracker(context_window=1_000)
    messages = (
        ChatMessage.system("system"),
        ChatMessage.user("你好"),
        ChatMessage.assistant("回答"),
    )
    tracker.record_request(
        TokenUsage(prompt_tokens=100, completion_tokens=20, cached_tokens=60),
        messages,
    )
    toolset = ContextToolset(tracker)
    report = toolset.get_context_stats()

    assert report == format_context_report(tracker.snapshot())
    assert "当前上下文：120 tok / 1,000" in report
    assert "最近一次缓存：命中 60 tok，未命中 40 tok" in report
    assert "命中率 60.0%" in report
    assert "消息数：3" in report

    assert [spec.name for spec in CONTEXT_TOOL_SPECS] == [
        "getContextStats",
        "recallContext",
    ]
    registry = ToolRegistry(toolset.registry_handlers(), CONTEXT_TOOL_SPECS[:1])
    result = registry.execute("getContextStats", "{}")
    assert result.ok
    assert "上下文用量" in result.content


def test_agent_emits_context_usage_with_cache(tmp_path: Path) -> None:
    usage = TokenUsage(
        prompt_tokens=100,
        completion_tokens=10,
        total_tokens=110,
        cached_tokens=60,
        cache_miss_tokens=40,
    )
    model = ScriptedModel([[ChatChunk(text="ok", usage=usage)]])
    toolset = FileToolset(tmp_path)
    agent = FileAgent(
        model=model,
        tools=ToolRegistry(toolset.registry_handlers(), FILE_TOOL_SPECS),
        system_prompt="test system",
    )

    async def scenario() -> list[Any]:
        return [event async for event in agent.send("hello")]

    events = asyncio.run(scenario())
    updates = [event.usage for event in events if isinstance(event, ContextStatsUpdated)]
    assert updates
    assert updates[0].estimated is True
    final = updates[-1]
    assert final.estimated is False
    assert final.context_tokens == 110
    assert final.total_cached_tokens == 60
    assert final.cache_hit_rate == pytest.approx(0.6)
    assert final.messages == 3
    assert isinstance(events[-1], TurnCompleted)
    assert agent.context_usage.context_tokens == 110

    agent.clear()
    cleared = agent.context_usage
    assert cleared.requests == 0
    assert cleared.messages == 1
    assert cleared.context_tokens == estimate_messages_tokens(agent.messages)


def test_agent_rollback_refreshes_context(tmp_path: Path) -> None:
    toolset = FileToolset(tmp_path)
    agent = FileAgent(
        model=FailingModel(),
        tools=ToolRegistry(toolset.registry_handlers(), FILE_TOOL_SPECS),
        system_prompt="test system",
    )

    async def scenario() -> list[Any]:
        return [event async for event in agent.send("hello")]

    events = asyncio.run(scenario())
    assert isinstance(events[-1], TurnFailed)
    assert agent.context_usage.messages == 1
    assert agent.context_usage.context_tokens == estimate_messages_tokens(agent.messages)


def test_agent_tool_sees_shared_context_stats(tmp_path: Path) -> None:
    tracker = ContextTracker(context_window=1_000)
    usage = TokenUsage(prompt_tokens=90, completion_tokens=10, cached_tokens=40)
    model = ScriptedModel(
        [
            [ChatChunk(text="第一轮", usage=usage)],
            [
                ChatChunk(
                    tool_call_deltas=(
                        ToolCallDelta(
                            index=0,
                            id="call_ctx",
                            name="getContextStats",
                            arguments="{}",
                        ),
                    )
                )
            ],
            [ChatChunk(text="查到了")],
        ]
    )
    file_tools = FileToolset(tmp_path)
    context_tools = ContextToolset(tracker)
    archive_tools = ContextArchiveToolset(ContextArchive(tmp_path))
    registry = ToolRegistry(
        file_tools.registry_handlers()
        | context_tools.registry_handlers()
        | archive_tools.registry_handlers(),
        FILE_TOOL_SPECS + CONTEXT_TOOL_SPECS,
    )
    agent = FileAgent(
        model=model,
        tools=registry,
        system_prompt="test system",
        context=tracker,
    )

    async def scenario() -> None:
        async for _ in agent.send("第一轮"):
            pass
        async for _ in agent.send("看看上下文"):
            pass

    asyncio.run(scenario())
    tool_messages = [
        message
        for message in model.calls[-1]
        if message.role == "tool"
    ]
    assert tool_messages
    assert "命中率 44.4%" in (tool_messages[-1].content or "")
    # 第二轮先发起工具调用，再在工具结果后发起总结请求，共 3 次请求。
    assert tracker.snapshot().requests == 3


class _FakeStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Any]:
        for chunk in self._chunks:
            yield chunk


class _FakeCompletions:
    def __init__(self, chunks: list[Any], *, reject_stream_options: bool = False) -> None:
        self._chunks = chunks
        self._reject = reject_stream_options
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        if self._reject and "stream_options" in kwargs:
            raise TypeError("stream_options is not supported")
        return _FakeStream(self._chunks)


def _model_with(fake: _FakeCompletions) -> OpenAIChatModel:
    model = OpenAIChatModel(LLMSettings(api_key="test", model="test"))
    model._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=fake)
    )
    return model


def test_openai_adapter_requests_and_emits_usage() -> None:
    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="hi", tool_calls=None)
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=12,
                completion_tokens=3,
                total_tokens=15,
                prompt_cache_hit_tokens=8,
                prompt_cache_miss_tokens=4,
            ),
        ),
    ]
    fake = _FakeCompletions(chunks)
    model = _model_with(fake)

    async def scenario() -> list[ChatChunk]:
        return [
            chunk
            async for chunk in model.stream([ChatMessage.user("hello")], ())
        ]

    result = asyncio.run(scenario())
    assert fake.calls[0]["stream_options"] == {"include_usage": True}
    assert result[0].text == "hi"
    assert result[-1].usage is not None
    assert result[-1].usage.prompt_tokens == 12
    assert result[-1].usage.cached_tokens == 8


def test_openai_adapter_falls_back_without_stream_options() -> None:
    fake = _FakeCompletions(
        [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="ok", tool_calls=None)
                    )
                ],
                usage=None,
            )
        ],
        reject_stream_options=True,
    )
    model = _model_with(fake)

    async def scenario() -> list[ChatChunk]:
        return [chunk async for chunk in model.stream([ChatMessage.user("hi")], ())]

    result = asyncio.run(scenario())
    assert len(fake.calls) == 2
    assert "stream_options" not in fake.calls[1]
    assert result[0].text == "ok"


def test_unknown_context_window_is_reported_without_config_fallback() -> None:
    tracker = ContextTracker()
    tracker.record_request(
        TokenUsage(prompt_tokens=10, completion_tokens=2),
        (ChatMessage.system("system"),),
    )
    snapshot = tracker.snapshot()
    assert snapshot.context_window == 0
    assert snapshot.window_known is False
    report = format_context_report(snapshot)
    assert "上游未返回上下文窗口" in report


def test_agent_compact_replaces_history_with_summary(tmp_path: Path) -> None:
    compact_usage = TokenUsage(prompt_tokens=50, completion_tokens=8, cached_tokens=20)
    model = ScriptedModel(
        [
            [ChatChunk(text="第一轮回答")],
            [
                ChatChunk(text="摘要：用户要求测试上下文压缩。"),
                ChatChunk(usage=compact_usage),
            ],
        ]
    )
    toolset = FileToolset(tmp_path)
    agent = FileAgent(
        model=model,
        tools=ToolRegistry(toolset.registry_handlers(), FILE_TOOL_SPECS),
        system_prompt="test system",
        archive=ContextArchive(tmp_path),
    )

    async def scenario() -> Any:
        async for _ in agent.send("第一轮"):
            pass
        return await agent.compact()

    result = asyncio.run(scenario())
    assert "摘要：用户要求测试上下文压缩。" in result.summary
    assert result.after_messages == 3
    assert result.before_messages == 3
    assert result.before_tokens > 0
    assert result.after_tokens > 0
    assert [message.role for message in agent.messages] == [
        "system",
        "user",
        "assistant",
    ]
    assert "压缩摘要" in (agent.messages[1].content or "")
    # 摘要末尾必须带归档说明，并指向 recallContext。
    assert result.archive_note in (agent.messages[1].content or "")
    assert "recallContext" in (agent.messages[1].content or "")
    assert result.archive_id
    archive_path = tmp_path / ".agents" / "context" / f"{result.archive_id}.jsonl"
    assert archive_path.is_file()
    records = [
        json.loads(line)
        for line in archive_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # 归档保留 system / user / assistant 的角色、内容与顺序。
    assert [record["role"] for record in records] == [
        "system",
        "user",
        "assistant",
    ]
    assert records[1]["content"] == "第一轮"
    assert agent.context_usage.total_prompt_tokens == 50
    assert agent.context_usage.requests == 2


def test_agent_compact_requires_archive(tmp_path: Path) -> None:
    toolset = FileToolset(tmp_path)
    agent = FileAgent(
        model=ScriptedModel([[ChatChunk(text="ok")], [ChatChunk(text="摘要")]]),
        tools=ToolRegistry(toolset.registry_handlers(), FILE_TOOL_SPECS),
        system_prompt="test system",
    )

    async def scenario() -> None:
        async for _ in agent.send("第一轮"):
            pass
        with pytest.raises(ValueError, match="归档"):
            await agent.compact()

    asyncio.run(scenario())
    # 未归档时不能替换历史。
    assert [message.role for message in agent.messages] == [
        "system",
        "user",
        "assistant",
    ]


def test_agent_compact_requires_history(tmp_path: Path) -> None:
    toolset = FileToolset(tmp_path)
    agent = FileAgent(
        model=ScriptedModel([]),
        tools=ToolRegistry(toolset.registry_handlers(), FILE_TOOL_SPECS),
        system_prompt="test system",
        archive=ContextArchive(tmp_path),
    )

    with pytest.raises(ValueError, match="没有可压缩"):
        asyncio.run(agent.compact())


def test_agent_compact_failure_keeps_history(tmp_path: Path) -> None:
    class FailingCompactModel:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[Mapping[str, Any]],
        ) -> AsyncIterator[ChatChunk]:
            self.calls += 1
            if self.calls == 1:
                yield ChatChunk(text="第一轮回答")
                return
            raise RuntimeError("summary boom")
            yield ChatChunk()  # pragma: no cover

    toolset = FileToolset(tmp_path)
    agent = FileAgent(
        model=FailingCompactModel(),
        tools=ToolRegistry(toolset.registry_handlers(), FILE_TOOL_SPECS),
        system_prompt="test system",
        archive=ContextArchive(tmp_path),
    )

    async def scenario() -> None:
        async for _ in agent.send("第一轮"):
            pass
        with pytest.raises(RuntimeError, match="summary boom"):
            await agent.compact()

    asyncio.run(scenario())
    assert [message.role for message in agent.messages] == [
        "system",
        "user",
        "assistant",
    ]
    assert agent.messages[1].content == "第一轮"
    # 归档已经先写入，方便用户/模型回溯失败前的原文。
    assert list((tmp_path / ".agents" / "context").glob("*.jsonl"))


def test_agent_blocks_prompts_after_threshold(tmp_path: Path) -> None:
    usage = TokenUsage(prompt_tokens=800, completion_tokens=100, total_tokens=900)
    model = ScriptedModel(
        [
            [ChatChunk(text="ok", usage=usage)],
            [ChatChunk(text="压缩后的摘要")],
            [ChatChunk(text="ok")],
        ]
    )
    tracker = ContextTracker(context_window=1_000)
    toolset = FileToolset(tmp_path)
    agent = FileAgent(
        model=model,
        tools=ToolRegistry(toolset.registry_handlers(), FILE_TOOL_SPECS),
        system_prompt="test system",
        context=tracker,
        archive=ContextArchive(tmp_path),
    )

    async def scenario() -> list[Any]:
        first = [event async for event in agent.send("第一轮")]
        # 达到 80% 后应发出一次警告，并拒绝下一轮普通提示词。
        warnings = [event for event in first if isinstance(event, ContextWarning)]
        assert len(warnings) == 1
        assert warnings[0].usage.needs_compaction
        assert agent.context_limit_reached
        with pytest.raises(ContextLimitError):
            [event async for event in agent.send("第二轮")]
        # 压缩后解除限制。
        await agent.compact()
        assert not agent.context_limit_reached
        return [event async for event in agent.send("第二轮")]

    events = asyncio.run(scenario())
    assert isinstance(events[-1], TurnCompleted)


def test_unknown_window_uses_128k_default_but_stays_flagged() -> None:
    tracker = ContextTracker()
    snapshot = tracker.record_request(
        TokenUsage(prompt_tokens=10, completion_tokens=2),
        (ChatMessage.system("system"),),
    )
    assert snapshot.window_known is False
    assert snapshot.window_assumed is True
    assert snapshot.effective_window == DEFAULT_CONTEXT_WINDOW
    assert snapshot.effective_ratio == pytest.approx(12 / DEFAULT_CONTEXT_WINDOW)
    assert snapshot.needs_compaction is False


def test_context_archive_recall_returns_snippets_and_positions(tmp_path: Path) -> None:
    archive = ContextArchive(tmp_path)
    messages = [
        ChatMessage.system("system prompt"),
        ChatMessage.user("请修改 core/context.py 里的 estimate_text_tokens"),
        ChatMessage.assistant("已读取 core/context.py，函数在第 30 行。"),
        ChatMessage.tool(
            "call_1",
            "git status --short\nM core/context.py\n?? notes.md",
        ),
    ]
    archived = archive.save(messages)
    result = archive.recall("estimate_text_tokens")
    assert result.total_matches == 1
    hit = result.hits[0]
    assert hit.archive_id == archived.archive_id
    assert hit.seq == 2
    assert "estimate_text_tokens" in hit.snippet

    # 工具结果里的命令输出同样可以被检索到。
    tool_hit = archive.recall("git status").hits[0]
    assert tool_hit.seq == 4
    assert tool_hit.role == "tool"

    # id 限定范围；不存在的编号给出可用列表。
    scoped = archive.recall("context.py", archived.archive_id)
    assert scoped.scanned_archives == 1
    assert all(hit.archive_id == archived.archive_id for hit in scoped.hits)
    with pytest.raises(ValueError, match="找不到归档"):
        archive.recall("anything", "20990101-000000")
    with pytest.raises(ValueError, match="不能为空"):
        archive.recall("   ")


def test_recall_context_tool_is_registered(tmp_path: Path) -> None:
    archive = ContextArchive(tmp_path)
    archive.save([ChatMessage.user("早期提到 secret-token-42")])
    toolset = ContextArchiveToolset(archive)
    registry = ToolRegistry(toolset.registry_handlers(), CONTEXT_TOOL_SPECS[1:])
    result = registry.execute("recallContext", '{"text": "secret-token-42"}')
    assert result.ok
    assert "secret-token-42" in result.content
    assert "第 1 条消息" in result.content

    missing = registry.execute("recallContext", "{}")
    assert missing.ok is False
    assert "text" in missing.content

    formatted = format_recall_report(archive.recall("secret-token-42"))
    assert formatted.startswith("上下文归档检索")


def test_archive_ids_are_unique_and_archives_are_searchable(tmp_path: Path) -> None:
    archive = ContextArchive(tmp_path)
    when = datetime(2026, 10, 22, 15, 30, 0)
    first = archive.save([ChatMessage.user("needle 1")], when=when)
    second = archive.save([ChatMessage.user("needle 2")], when=when)
    assert first.archive_id == "20261022-153000"
    assert second.archive_id == "20261022-153000-2"
    assert len(archive.archive_ids()) == 2

    # 未指定 id 时检索全部归档，最新的记录排在前面。
    result = archive.recall("needle")
    assert result.scanned_archives == 2
    assert {hit.archive_id for hit in result.hits} == {
        first.archive_id,
        second.archive_id,
    }

    # 结果条数与总长度都受上限保护。
    many = archive.save(
        [ChatMessage.user(f"overflow-{index}") for index in range(10)]
    )
    limited = archive.recall("overflow", archive_id=many.archive_id, limit=3)
    assert len(limited.hits) == 3
    assert limited.truncated is True
    clipped = archive.recall("overflow", archive_id=many.archive_id, max_chars=10)
    # 单条片段允许超过上限（否则永远没有结果），但不会继续追加第二条。
    assert len(clipped.hits) == 1


def test_agent_refine_prompt_does_not_touch_history(tmp_path: Path) -> None:
    model = ScriptedModel(
        [[ChatChunk(text="```\n润色后：请读取 README.md 并总结要点。\n```")]]
    )
    toolset = FileToolset(tmp_path)
    agent = FileAgent(
        model=model,
        tools=ToolRegistry(toolset.registry_handlers(), FILE_TOOL_SPECS),
        system_prompt="test system",
    )

    refined = asyncio.run(agent.refine_prompt("看看 readme"))
    assert refined == "请读取 README.md 并总结要点。"
    assert len(agent.messages) == 1
    assert agent.context_usage.requests == 0
    assert len(model.calls) == 1
    assert [message.role for message in model.calls[0]] == ["system", "user"]
    assert model.calls[0][1].content == "看看 readme"


