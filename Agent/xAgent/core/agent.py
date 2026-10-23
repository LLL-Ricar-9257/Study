"""Agent 编排：模型流 -> 工具循环 -> 领域事件。

核心类不读 stdin、不 print、不依赖 Textual。任何 UI 都可以通过
``async for event in agent.send(text)`` 驱动同一套业务逻辑。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .errors import AgentBusyError
from .events import (
    AgentEvent,
    TextDelta,
    ToolFinished,
    ToolRoundLimitReached,
    ToolStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from .models import ChatChunk, ChatMessage, ToolCall
from .ports import ChatModel
from .tools.registry import ToolRegistry


@dataclass(slots=True)
class _PendingToolCall:
    id: str = ""
    name: str = ""
    arguments: str = ""

    def freeze(self, fallback_id: str) -> ToolCall:
        return ToolCall(
            id=self.id or fallback_id,
            name=self.name,
            arguments=self.arguments,
        )


class FileAgent:
    """一个独立会话的文件管理 Agent。"""

    def __init__(
        self,
        *,
        model: ChatModel,
        tools: ToolRegistry,
        system_prompt: str,
        max_tool_rounds: int = 20,
    ) -> None:
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds 必须大于等于 1")
        self._model = model
        self._tools = tools
        self._system_prompt = system_prompt
        self._max_tool_rounds = max_tool_rounds
        self._messages: list[ChatMessage] = [ChatMessage.system(system_prompt)]
        self._lock = asyncio.Lock()

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        """当前对话快照（含 system 消息），供 UI 恢复/调试使用。"""
        return tuple(self._messages)

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    def clear(self) -> None:
        """清空对话历史，保留 system 提示。忙碌时拒绝操作。"""
        if self._lock.locked():
            raise AgentBusyError("Agent 正在处理当前请求，暂时不能清空对话")
        self._messages = [ChatMessage.system(self._system_prompt)]

    async def send(self, user_text: str) -> AsyncIterator[AgentEvent]:
        """处理一轮用户输入，并流式产生 UI 事件。

        通过 async generator 形成背压：UI 处理完一个事件后才会继续拉取。
        发生模型错误或任务被取消时，本轮消息会从历史中回滚。
        """
        text = (user_text or "").strip()
        if not text:
            raise ValueError("用户输入不能为空")
        if self._lock.locked():
            raise AgentBusyError("Agent 正在处理上一轮请求")

        async with self._lock:
            turn_start = len(self._messages)
            self._messages.append(ChatMessage.user(text))
            active_turn = True
            try:
                yield TurnStarted(text)
                async for event in self._run_turn():
                    if isinstance(event, (TurnCompleted, TurnFailed)):
                        active_turn = False
                    yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                del self._messages[turn_start:]
                active_turn = False
                yield TurnFailed(
                    message=f"本轮对话失败：{exc}",
                    detail=type(exc).__name__,
                )
            finally:
                if active_turn:
                    # GeneratorExit / 取消：确保不留下半截会话。
                    del self._messages[turn_start:]

    async def aclose(self) -> None:
        close = getattr(self._model, "aclose", None)
        if callable(close):
            await close()

    async def _run_turn(self) -> AsyncIterator[AgentEvent]:
        for round_index in range(self._max_tool_rounds):
            text_parts: list[str] = []
            pending: dict[int, _PendingToolCall] = {}

            async for chunk in self._model.stream(
                tuple(self._messages),
                tuple(self._tools.schema()),
            ):
                async for event in self._consume_chunk(chunk, text_parts, pending):
                    yield event

            text = "".join(text_parts)
            calls = tuple(
                pending[index].freeze(f"call_{round_index}_{index}")
                for index in sorted(pending)
                if pending[index].name or pending[index].arguments
            )
            self._messages.append(
                ChatMessage.assistant(text or None, tool_calls=calls)
            )

            if not calls:
                yield TurnCompleted(text)
                return

            for call in calls:
                yield ToolStarted(
                    call_id=call.id,
                    name=call.name,
                    arguments=call.arguments,
                )
                started = time.perf_counter()
                result = await asyncio.to_thread(
                    self._tools.execute, call.name, call.arguments
                )
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                self._messages.append(ChatMessage.tool(call.id, result.content))
                yield ToolFinished(
                    call_id=call.id,
                    name=call.name,
                    ok=result.ok,
                    content=result.content,
                    elapsed_ms=elapsed_ms,
                )

        message = (
            f"工具调用次数达到上限（{self._max_tool_rounds} 轮），已停止本轮操作。"
        )
        self._messages.append(ChatMessage.assistant(message))
        yield ToolRoundLimitReached(limit=self._max_tool_rounds, message=message)
        yield TurnCompleted(message, limit_reached=True)

    async def _consume_chunk(
        self,
        chunk: ChatChunk,
        text_parts: list[str],
        pending: dict[int, _PendingToolCall],
    ) -> AsyncIterator[AgentEvent]:
        if chunk.text:
            text_parts.append(chunk.text)
            yield TextDelta(chunk.text)

        for delta in chunk.tool_call_deltas:
            entry = pending.setdefault(delta.index, _PendingToolCall())
            if delta.id:
                entry.id += delta.id
            if delta.name:
                entry.name += delta.name
            if delta.arguments:
                entry.arguments += delta.arguments
