from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from core.agent import FileAgent
from core.config import AgentLimits
from core.errors import AgentBusyError
from core.events import (
    TextDelta,
    ToolFinished,
    ToolRoundLimitReached,
    ToolStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from core.models import ChatChunk, ChatMessage, ToolCallDelta
from core.tools.filesystem import FileToolset
from core.tools.registry import ToolRegistry
from core.tools.specs import FILE_TOOL_SPECS


class ScriptedModel:
    """按脚本逐轮返回流式分片，并记录收到的消息。"""

    def __init__(self, scripts: list[list[ChatChunk]]) -> None:
        self.scripts = scripts
        self.calls: list[tuple[ChatMessage, ...]] = []
        self.closed = False

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatChunk]:
        self.calls.append(tuple(messages))
        script = self.scripts.pop(0)
        for chunk in script:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class FailingModel:
    async def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatChunk]:
        raise RuntimeError("boom")
        yield ChatChunk()  # pragma: no cover


def _agent(tmp_path: Path, model: Any, max_rounds: int = 20) -> FileAgent:
    toolset = FileToolset(tmp_path, limits=AgentLimits())
    return FileAgent(
        model=model,
        tools=ToolRegistry(toolset.registry_handlers(), FILE_TOOL_SPECS),
        system_prompt="test system",
        max_tool_rounds=max_rounds,
    )


def test_agent_runs_tool_then_answers(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            [
                ChatChunk(
                    tool_call_deltas=(
                        ToolCallDelta(
                            index=0,
                            id="call_1",
                            name="create_directory",
                            arguments='{"path":',
                        ),
                    )
                ),
                ChatChunk(
                    tool_call_deltas=(
                        ToolCallDelta(index=0, arguments=' "notes"}'),
                    )
                ),
            ],
            [ChatChunk(text="已创建目录。")],
        ]
    )
    agent = _agent(tmp_path, model)

    async def scenario() -> list[Any]:
        return [event async for event in agent.send("新建 notes 目录")]

    events = asyncio.run(scenario())
    assert isinstance(events[0], TurnStarted)
    assert any(isinstance(event, ToolStarted) for event in events)
    finished = next(event for event in events if isinstance(event, ToolFinished))
    assert finished.ok
    assert (tmp_path / "notes").is_dir()
    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].content == "已创建目录。"
    assert any(isinstance(event, TextDelta) for event in events)
    assert len(model.calls) == 2
    assert model.calls[0][-1].role == "user"
    assert any(message.role == "tool" for message in model.calls[1])


def test_model_failure_rolls_back_turn(tmp_path: Path) -> None:
    agent = _agent(tmp_path, FailingModel())

    async def scenario() -> list[Any]:
        return [event async for event in agent.send("hello")]

    events = asyncio.run(scenario())
    assert isinstance(events[-1], TurnFailed)
    assert len(agent.messages) == 1
    assert agent.messages[0].role == "system"


def test_busy_agent_rejects_concurrent_turn(tmp_path: Path) -> None:
    model = ScriptedModel([[ChatChunk(text="ok")], [ChatChunk(text="again")]])
    agent = _agent(tmp_path, model)

    async def scenario() -> None:
        first = agent.send("one")
        await anext(first)
        with pytest.raises(AgentBusyError):
            await anext(agent.send("two"))
        await first.aclose()

    asyncio.run(scenario())
    assert len(agent.messages) == 1


def test_tool_round_limit(tmp_path: Path) -> None:
    script = [
        ChatChunk(
            tool_call_deltas=(
                ToolCallDelta(
                    index=0,
                    id="call_x",
                    name="get_current_directory",
                    arguments="{}",
                ),
            )
        )
    ]
    model = ScriptedModel([script, script])
    agent = _agent(tmp_path, model, max_rounds=2)

    async def scenario() -> list[Any]:
        return [event async for event in agent.send("loop")]

    events = asyncio.run(scenario())
    assert sum(isinstance(event, ToolStarted) for event in events) == 2
    assert any(isinstance(event, ToolRoundLimitReached) for event in events)
    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].limit_reached


def test_clear_and_close(tmp_path: Path) -> None:
    model = ScriptedModel([[ChatChunk(text="ok")]])
    agent = _agent(tmp_path, model)

    async def scenario() -> None:
        async for _ in agent.send("hello"):
            pass
        assert len(agent.messages) == 3
        agent.clear()
        assert len(agent.messages) == 1
        await agent.aclose()

    asyncio.run(scenario())
    assert model.closed
