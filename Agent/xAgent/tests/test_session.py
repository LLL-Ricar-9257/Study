from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from core.agent import FileAgent
from core.archive import ContextArchive
from core.bootstrap import build_agent
from core.config import AgentLimits, AppConfig, LLMSettings
from core.mcp import McpSettings
from core.models import ChatChunk, ChatMessage
from core.session import (
    SessionNotFoundError,
    SessionStore,
    agents_md_message,
    is_agents_md_message,
    load_agents_md,
)
from core.tools.filesystem import FileToolset
from core.tools.registry import ToolRegistry
from core.tools.specs import FILE_TOOL_SPECS


class ScriptedModel:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls: list[tuple[ChatMessage, ...]] = []

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatChunk]:
        self.calls.append(tuple(messages))
        yield ChatChunk(text=self.texts.pop(0))

    async def aclose(self) -> None:
        return None


def _agent(tmp_path: Path, model: Any, **kwargs: Any) -> FileAgent:
    return FileAgent(
        model=model,
        tools=ToolRegistry(FileToolset(tmp_path).registry_handlers(), FILE_TOOL_SPECS),
        system_prompt="test system",
        archive=ContextArchive(tmp_path),
        session_store=SessionStore(tmp_path),
        **kwargs,
    )


def test_session_store_save_load_and_list(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.new_id()
    messages = [
        ChatMessage.system("sys"),
        ChatMessage.user("请修改 README"),
        ChatMessage.assistant("好的"),
        ChatMessage.tool("call_1", "工具输出"),
    ]
    saved = store.save(
        session_id,
        messages,
        title="",
        workspace=str(tmp_path),
        agents_md_path="AGENTS.md",
    )
    assert saved.path.is_file()
    assert saved.title == "请修改 README"

    loaded = store.load(session_id)
    assert [message.role for message in loaded.messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert loaded.messages[3].content == "工具输出"
    assert loaded.agents_md_path == "AGENTS.md"

    listed = store.list_sessions()
    assert [item.session_id for item in listed] == [session_id]
    assert listed[0].message_count == 4

    with pytest.raises(SessionNotFoundError):
        store.load("20990101-000000")
    with pytest.raises(SessionNotFoundError):
        store.load("../evil")


def test_agents_md_message_marker() -> None:
    message = agents_md_message("规则一")
    assert is_agents_md_message(message)
    assert "规则一" in (message.content or "")
    assert not is_agents_md_message(ChatMessage.user("普通问题"))


def test_agent_injects_agents_md_once_and_persists(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# 项目规则\n\n始终使用中文。", encoding="utf-8")
    document = load_agents_md(tmp_path)
    assert document is not None
    model = ScriptedModel(["第一轮回答", "第二轮回答"])
    agent = _agent(
        tmp_path,
        model,
        agents_md=document,
        agents_md_loader=lambda: load_agents_md(tmp_path),
    )

    assert len(agent.messages) == 2
    assert is_agents_md_message(agent.messages[1])
    assert "始终使用中文" in (agent.messages[1].content or "")

    async def scenario() -> None:
        async for _ in agent.send("第一轮"):
            pass
        async for _ in agent.send("第二轮"):
            pass

    asyncio.run(scenario())
    # 注入只发生一次：第二轮请求里只有一条 AGENTS.md 消息。
    injected = [
        message for message in model.calls[-1] if is_agents_md_message(message)
    ]
    assert len(injected) == 1

    summary = agent.save_session()
    assert summary is not None
    log = summary.path.read_text(encoding="utf-8")
    assert '"type": "agents_md"' in log
    assert "始终使用中文" in log

    loaded = agent.session_store.load(summary.session_id)
    assert any(is_agents_md_message(message) for message in loaded.messages)

    # 恢复会话不重复注入。
    agent.resume_session(summary.session_id)
    assert len(
        [message for message in agent.messages if is_agents_md_message(message)]
    ) == 1


def test_new_session_reloads_agents_md_without_saving_when_empty(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text("旧规则", encoding="utf-8")
    model = ScriptedModel(["回答"])
    agent = _agent(
        tmp_path,
        model,
        agents_md=load_agents_md(tmp_path),
        agents_md_loader=lambda: load_agents_md(tmp_path),
    )
    first_id = agent.session_id

    async def scenario() -> None:
        async for _ in agent.send("第一轮"):
            pass

    asyncio.run(scenario())
    assert agent.save_session() is not None

    (tmp_path / "AGENTS.md").write_text("新规则", encoding="utf-8")
    old = agent.new_session()
    assert old is not None
    assert agent.session_id != first_id
    assert len(agent.messages) == 2
    assert "新规则" in (agent.messages[1].content or "")

    assert agent.session_store.list_sessions()[0].session_id == first_id
    # 刚新建的空会话不会被重复保存成新日志。
    assert agent.save_session() is None


def test_compact_keeps_agents_md_message(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("项目规则：始终运行测试。", encoding="utf-8")
    model = ScriptedModel(["回答", "压缩摘要"])
    agent = _agent(
        tmp_path,
        model,
        agents_md=load_agents_md(tmp_path),
        agents_md_loader=lambda: load_agents_md(tmp_path),
    )

    async def scenario() -> None:
        async for _ in agent.send("第一轮"):
            pass
        await agent.compact()

    asyncio.run(scenario())
    roles = [message.role for message in agent.messages]
    assert roles == ["system", "user", "user", "assistant"]
    assert is_agents_md_message(agent.messages[1])
    assert "压缩摘要" in (agent.messages[2].content or "")


def test_build_agent_injects_project_agents_md(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# 项目规则\n使用 pytest。", encoding="utf-8")
    config = AppConfig(
        workspace=tmp_path,
        llm=LLMSettings(api_key="test-key"),
        limits=AgentLimits(),
    )
    agent = build_agent(
        config,
        project_root=tmp_path,
        bundled_roots=(),
        mcp_settings=McpSettings(enabled=False),
    )
    try:
        assert agent.agents_md is not None
        assert agent.agents_md.path == (tmp_path / "AGENTS.md").resolve()
        assert is_agents_md_message(agent.messages[1])
        assert "使用 pytest" in (agent.messages[1].content or "")
    finally:
        asyncio.run(agent.aclose())


def test_session_log_is_valid_jsonl(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.new_id()
    summary = store.save(session_id, [ChatMessage.user("hello")])
    records = [
        json.loads(line)
        for line in summary.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records[0]["type"] == "session"
    assert records[1]["type"] == "message"
    assert records[1]["message"]["content"] == "hello"
