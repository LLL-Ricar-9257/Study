"""Agent 编排：模型流 -> 工具循环 -> 领域事件。

核心类不读 stdin、不 print、不依赖 Textual。任何 UI 都可以通过
``async for event in agent.send(text)`` 驱动同一套业务逻辑。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .archive import ContextArchive
from .compaction import (
    build_archive_notice,
    build_compaction_messages,
    build_summary_message,
    render_display_transcript,
    render_transcript,
)
from .config import ContextSettings
from .context import ContextTracker, ContextUsage
from .errors import AgentBusyError, ContextLimitError
from .events import (
    AgentEvent,
    ContextStatsUpdated,
    ContextWarning,
    TextDelta,
    ToolFinished,
    ToolRoundLimitReached,
    ToolStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from .models import ChatChunk, ChatMessage, TokenUsage, ToolCall
from .ports import ChatModel
from .session import (
    AgentsDocument,
    LoadedSession,
    SessionStore,
    SessionSummary,
    agents_md_message,
    is_agents_md_message,
)
from .tools.registry import ToolRegistry
from .tools.types import ToolSpec

if TYPE_CHECKING:
    from .mcp.manager import McpClientManager, McpServerSummary

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """一次上下文压缩的结果，供 UI 展示压缩前后的规模。"""

    summary: str
    before_messages: int
    after_messages: int
    before_tokens: int
    after_tokens: int
    archive_id: str = ""
    archive_path: str = ""
    archive_note: str = ""
    created_at: str = ""
    before_text: str = ""
    """压缩前原文（已按展示需要截断），供对照面板左栏使用。"""

    after_text: str = ""
    """替换后的历史正文，供对照面板右栏使用。"""


@dataclass(frozen=True, slots=True)
class McpLoadResult:
    """后台加载 MCP 的结果，供 UI 刷新侧栏。"""

    summaries: tuple[McpServerSummary, ...] = ()
    notices: tuple[str, ...] = ()
    tool_count: int = 0
    error: str = ""


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


_REFINE_SYSTEM_PROMPT = (
    "你是一名提示词优化助手。请把用户给出的草稿改写成清晰、具体、可执行的中文提示词。\n"
    "要求：保留原意，补充必要的目标、输入、约束和期望输出；不要回答草稿中的问题；"
    "不要添加与任务无关的内容。\n"
    "只输出改写后的提示词正文，不要加引号、代码围栏或“润色后：”之类的标签。"
)


def _clean_refined_prompt(text: str) -> str:
    """去掉模型可能包裹在结果外层的代码围栏或前缀标签。"""

    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    for prefix in ("润色后的提示词：", "润色后的提示词:", "润色后：", "提示词："):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned


class FileAgent:
    """一个独立会话的文件管理 Agent。"""

    def __init__(
        self,
        *,
        model: ChatModel,
        tools: ToolRegistry,
        system_prompt: str,
        max_tool_rounds: int | None = None,
        startup_notices: Iterable[str] | None = None,
        cleanup: Iterable[Callable[[], Any]] | None = None,
        context: ContextTracker | None = None,
        archive: ContextArchive | None = None,
        context_settings: ContextSettings | None = None,
        session_store: SessionStore | None = None,
        session_id: str | None = None,
        workspace: str | Path | None = None,
        agents_md: AgentsDocument | None = None,
        agents_md_loader: Callable[[], AgentsDocument | None] | None = None,
        mcp_manager: McpClientManager | None = None,
    ) -> None:
        """创建会话。

        ``max_tool_rounds`` 为 ``None`` 或 0 时不限制单轮对话的工具调用轮数，
        直到模型不再请求工具或用户取消；显式传入正整数时才启用上限。

        ``startup_notices`` 是启动阶段的连接状态/错误提示，供 UI 展示；
        ``cleanup`` 是不属于模型端口的可关闭资源（例如 MCP 客户端管理器），
        ``aclose()`` 时依次释放，单个释放失败不会影响其他资源。
        ``context`` 是上下文用量跟踪器；传入共享实例时，``getContextStats``
        工具和 Agent 看到同一份数据。省略时创建仅属于本会话的跟踪器。
        ``archive`` 是上下文归档目录；压缩前必须成功写入归档，未配置时
        ``compact()`` 会直接拒绝执行，避免出现无法回溯的压缩。
        ``session_store`` 是会话事件日志目录；配置后 ``/save``、``/new``、
        ``/resume`` 才能工作。``agents_md`` 会在本会话首次加载时注入一次。
        ``mcp_manager`` 是尚未启动的 MCP 管理器，由 :meth:`start_mcp` 在后台
        加载，避免阻塞 TUI 首屏。
        """
        if max_tool_rounds is not None:
            if max_tool_rounds < 0:
                raise ValueError(
                    "max_tool_rounds 不能为负数，或使用 None / 0 表示不限制"
                )
            if max_tool_rounds == 0:
                max_tool_rounds = None
        self._model = model
        self._tools = tools
        self._system_prompt = system_prompt
        self._max_tool_rounds = max_tool_rounds
        self._startup_notices = tuple(startup_notices or ())
        self._cleanup = tuple(cleanup or ())
        self._messages: list[ChatMessage] = [ChatMessage.system(system_prompt)]
        self._lock = asyncio.Lock()
        self._context = context or ContextTracker()
        self._archive = archive
        self._context_settings = context_settings or ContextSettings()
        self._context.reset(self._messages)
        self._context_warned = False

        self._session_store = session_store
        self._workspace = str(Path(workspace).resolve()) if workspace else ""
        self._session_id = session_id or (
            session_store.new_id() if session_store else ""
        )
        self._session_created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._session_title = ""
        self._agents_md = agents_md
        self._agents_md_loader = agents_md_loader
        self._inject_agents_md(agents_md)

        self._mcp_manager = mcp_manager
        self._mcp_started = False
        self._mcp_result: McpLoadResult | None = None

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        """当前对话快照（含 system 消息），供 UI 恢复/调试使用。"""
        return tuple(self._messages)

    @property
    def context_usage(self) -> ContextUsage:
        """当前上下文与缓存用量快照，供 UI 首屏/清空后直接读取。"""
        return self._context.snapshot()

    @property
    def context_limit_reached(self) -> bool:
        """上下文是否已达到压缩阈值（此时普通提示词会被拒绝）。"""

        return self._context.snapshot().needs_compaction

    @property
    def archive(self) -> ContextArchive | None:
        """本会话使用的上下文归档目录；未配置时为 ``None``。"""
        return self._archive

    @property
    def system_prompt(self) -> str:
        """本会话构造时写入 system 消息的提示词，供 UI 运行时展示。"""
        return self._system_prompt

    @property
    def startup_notices(self) -> tuple[str, ...]:
        """启动阶段的连接状态或非致命错误，供 UI 在会话区提示用户。"""
        return self._startup_notices

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """本轮会话可用的全部工具声明（含本地工具与 MCP 工具）。"""
        return self._tools.specs

    @property
    def session_id(self) -> str:
        """当前会话的事件日志编号；未配置会话目录时为空字符串。"""
        return self._session_id

    @property
    def session_title(self) -> str:
        return self._session_title

    @property
    def session_store(self) -> SessionStore | None:
        return self._session_store

    @property
    def agents_md(self) -> AgentsDocument | None:
        """本会话注入过的 AGENTS.md；未注入时为 ``None``。"""
        return self._agents_md

    @property
    def mcp_pending(self) -> bool:
        """MCP 是否已配置但尚未在后台加载完成。"""
        return self._mcp_manager is not None and not self._mcp_started

    @property
    def mcp_result(self) -> McpLoadResult | None:
        return self._mcp_result

    @property
    def mcp_summaries(self) -> tuple[McpServerSummary, ...]:
        """当前 MCP 服务器摘要；未配置或尚未加载时为空。"""

        if self._mcp_manager is None:
            return ()
        return tuple(self._mcp_manager.summaries)

    def _inject_agents_md(self, document: AgentsDocument | None) -> None:
        """把 AGENTS.md 作为一条历史消息注入；每个会话只做一次。"""

        if document is None or not document.content.strip():
            return
        self._agents_md = document
        self._messages.append(agents_md_message(document.content))

    def _has_conversation(self) -> bool:
        return any(
            message.role != "system" and not is_agents_md_message(message)
            for message in self._messages
        )

    def clear(self) -> None:
        """清空对话历史，保留 system 提示与 AGENTS.md 项目指令。"""

        if self._lock.locked():
            raise AgentBusyError("Agent 正在处理当前请求，暂时不能清空对话")
        self._messages = [ChatMessage.system(self._system_prompt)]
        self._inject_agents_md(self._agents_md)
        self._context.reset(self._messages)
        self._context_warned = False
        self._session_title = ""

    def save_session(self) -> SessionSummary | None:
        """把当前会话写入事件日志；没有实际对话时返回 ``None``。"""

        if self._lock.locked():
            raise AgentBusyError("Agent 正在处理当前请求，暂时不能保存会话")
        store = self._session_store
        if store is None:
            raise ValueError("未配置会话目录，无法保存会话")
        if not self._has_conversation():
            return None
        summary = store.save(
            self._session_id,
            self._messages,
            created_at=self._session_created_at,
            title=self._session_title,
            workspace=self._workspace,
            agents_md_path=str(self._agents_md.path) if self._agents_md else "",
        )
        if not self._session_title:
            self._session_title = summary.title
        return summary

    def new_session(self, *, save_current: bool = True) -> SessionSummary | None:
        """保存当前会话并开始一个新会话；返回刚保存的会话摘要。"""

        if self._lock.locked():
            raise AgentBusyError("Agent 正在处理当前请求，暂时不能新建会话")
        saved = self.save_session() if save_current else None
        store = self._session_store
        self._session_id = store.new_id() if store else ""
        self._session_created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._session_title = ""
        self._messages = [ChatMessage.system(self._system_prompt)]
        if self._agents_md_loader is not None:
            self._agents_md = self._agents_md_loader()
        self._inject_agents_md(self._agents_md)
        self._context.reset(self._messages)
        self._context_warned = False
        return saved

    def resume_session(self, session_id: str) -> LoadedSession:
        """从事件日志恢复会话；不重复注入 AGENTS.md。"""

        if self._lock.locked():
            raise AgentBusyError("Agent 正在处理当前请求，暂时不能恢复会话")
        store = self._session_store
        if store is None:
            raise ValueError("未配置会话目录，无法恢复会话")
        loaded = store.load(session_id)
        messages = list(loaded.messages)
        if messages and messages[0].role == "system":
            messages[0] = ChatMessage.system(self._system_prompt)
        else:
            messages.insert(0, ChatMessage.system(self._system_prompt))
        self._messages = messages
        self._session_id = loaded.session_id
        self._session_created_at = loaded.created_at or self._session_created_at
        self._session_title = loaded.title
        self._context.reset(self._messages)
        self._context_warned = False
        return loaded

    def rename_session(self, title: str) -> str:
        text = (title or "").strip()
        if not text:
            raise ValueError("会话标题不能为空")
        self._session_title = text[:80]
        return self._session_title

    async def start_mcp(self) -> McpLoadResult:
        """在后台线程连接 MCP Server，并把发现的工具动态注册进来。

        重复调用只会返回第一次的结果；没有配置 MCP 时返回空结果。
        """

        if self._mcp_manager is None:
            return McpLoadResult()
        if self._mcp_started and self._mcp_result is not None:
            return self._mcp_result
        manager = self._mcp_manager
        try:
            await asyncio.to_thread(manager.start)
            self._tools.register(
                manager.registry_handlers(),
                manager.tool_specs(),
            )
            result = McpLoadResult(
                summaries=tuple(manager.summaries),
                notices=tuple(manager.notices()),
                tool_count=len(manager.tool_specs()),
            )
        except Exception as exc:  # 加载失败不能影响本地工具
            result = McpLoadResult(error=f"{type(exc).__name__}: {exc}")
        self._mcp_started = True
        self._mcp_result = result
        return result

    async def compact(self, *, archive_id: str | None = None) -> CompactionResult:
        """压缩上下文：归档原文 -> 模型生成摘要 -> 用摘要替换历史。

        流程严格按顺序执行：

        1. 先把完整原始上下文写入 ``.agents/context/<编号>.jsonl``；写入失败
           直接抛异常，历史不做任何改动。
        2. 只把历史记录交给模型生成摘要，请求不携带任何工具，因此压缩期间
           不会触发 Function Calling。
        3. 摘要成功后，保留原 System Prompt，把摘要（末尾附归档说明）作为
           唯一的历史消息写回。

        任何一步失败都会保留原有上下文，用户可以重新执行 ``/compact``。
        """

        if self._lock.locked():
            raise AgentBusyError("Agent 正在处理当前请求，暂时不能压缩上下文")
        async with self._lock:
            if not self._has_conversation():
                raise ValueError("当前还没有可压缩的对话")
            archive = self._archive
            if archive is None:
                raise ValueError("未配置上下文归档目录，无法执行上下文压缩")

            before = self._context.snapshot()
            settings = self._context_settings
            before_text = render_display_transcript(
                self._messages,
                total_chars=settings.display_transcript_chars,
            )

            # 1) 原始上下文必须先完整落盘，归档失败就不继续压缩。
            archived = archive.save(self._messages, archive_id=archive_id)

            # 2) 专用 Prompt + 不传工具，压缩期间只允许这一次模型调用。
            request = build_compaction_messages(
                render_transcript(
                    self._messages[1:],
                    max_message_chars=settings.max_message_transcript_chars,
                    max_tool_chars=settings.max_tool_transcript_chars,
                    total_chars=settings.model_transcript_chars,
                ),
                archive_id=archived.archive_id,
                created_at=archived.created_at,
                target_tokens=settings.summary_tokens,
            )
            summary, usage = await self._collect_text(request)
            summary = summary.strip()
            if not summary:
                raise ValueError("模型没有返回上下文摘要")

            # 3) System Prompt 与工具定义保持不变，只替换历史消息。
            notice = build_archive_notice(archived.archive_id, archived.created_at)
            replacement = [ChatMessage.system(self._system_prompt)]
            # AGENTS.md 是项目级指令，不随对话压缩丢失：原样保留在历史最前面。
            injected = next(
                (
                    message
                    for message in self._messages
                    if is_agents_md_message(message)
                ),
                None,
            )
            if injected is None and self._agents_md is not None:
                injected = agents_md_message(self._agents_md.content)
            if injected is not None:
                replacement.append(injected)
            replacement.extend(
                (
                    build_summary_message(summary, notice),
                    ChatMessage.assistant("已了解此前对话的摘要，可以继续。"),
                )
            )
            self._messages = replacement
            if usage is not None:
                self._context.record_request(usage, self._messages)
            after = self._context.refresh(self._messages)
            self._context_warned = False
            return CompactionResult(
                summary=summary,
                before_messages=before.messages,
                after_messages=after.messages,
                before_tokens=before.context_tokens,
                after_tokens=after.context_tokens,
                archive_id=archived.archive_id,
                archive_path=str(archived.path),
                archive_note=notice,
                created_at=archived.created_at,
                before_text=before_text,
                after_text=render_display_transcript(
                    self._messages[1:],
                    total_chars=settings.display_transcript_chars,
                ),
            )

    async def refine_prompt(self, draft: str) -> str:
        """把用户的提示词草稿改写得更清晰、具体。

        该请求不写入对话历史，只把改写结果返回给调用方（TUI 会回填到输入框），
        避免润色动作污染真正的会话上下文。
        """

        text = (draft or "").strip()
        if not text:
            raise ValueError("待润色的提示词不能为空")
        if self._lock.locked():
            raise AgentBusyError("Agent 正在处理当前请求，暂时不能润色提示词")
        async with self._lock:
            request = (
                ChatMessage.system(_REFINE_SYSTEM_PROMPT),
                ChatMessage.user(text),
            )
            refined, usage = await self._collect_text(request)
            refined = _clean_refined_prompt(refined)
            if not refined:
                raise ValueError("模型没有返回润色结果")
            if usage is not None:
                # 只累计润色请求的 token，不改变当前对话历史。
                self._context.record_request(usage, self._messages)
                self._context.refresh(self._messages)
            return refined

    async def _collect_text(
        self,
        messages: Sequence[ChatMessage],
    ) -> tuple[str, TokenUsage | None]:
        """调用模型并收集纯文本输出；用于压缩、润色等辅助请求。"""

        parts: list[str] = []
        usage: TokenUsage | None = None
        async for chunk in self._model.stream(tuple(messages), ()):
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.text:
                parts.append(chunk.text)
        return "".join(parts).strip(), usage

    async def send(self, user_text: str) -> AsyncIterator[AgentEvent]:
        """处理一轮用户输入，并流式产生 UI 事件。

        通过 async generator 形成背压：UI 处理完一个事件后才会继续拉取。
        发生模型错误或任务被取消时，本轮消息会从历史中回滚。
        """
        text = (user_text or "").strip()
        if not text:
            raise ValueError("用户输入不能为空")
        snapshot = self._context.snapshot()
        if snapshot.needs_compaction:
            raise ContextLimitError(
                "上下文已达到窗口的 "
                f"{snapshot.warning_ratio:.0%}，已暂停接受新的普通提示词。"
                "请先执行 /compact 压缩上下文。"
            )
        if self._lock.locked():
            raise AgentBusyError("Agent 正在处理上一轮请求")

        async with self._lock:
            turn_start = len(self._messages)
            self._messages.append(ChatMessage.user(text))
            active_turn = True
            try:
                yield TurnStarted(text)
                yield ContextStatsUpdated(
                    self._context.refresh(self._messages)
                )
                async for event in self._run_turn():
                    if isinstance(event, (TurnCompleted, TurnFailed)):
                        active_turn = False
                    yield event
                    warning = self._context_warning_event()
                    if warning is not None:
                        yield warning
            except asyncio.CancelledError:
                self._context.refresh(self._messages)
                raise
            except Exception as exc:
                del self._messages[turn_start:]
                active_turn = False
                self._context.refresh(self._messages)
                logger.warning(
                    "turn_failed",
                    extra={"error": type(exc).__name__, "session": self._session_id},
                )
                yield ContextStatsUpdated(self._context.snapshot())
                yield TurnFailed(
                    message=f"本轮对话失败：{exc}",
                    detail=type(exc).__name__,
                )
            finally:
                if active_turn:
                    # GeneratorExit / 取消：确保不留下半截会话。
                    del self._messages[turn_start:]
                    self._context.refresh(self._messages)

    def _context_warning_event(self) -> ContextWarning | None:
        """上下文首次达到 80% 时返回一次警告事件。"""

        usage = self._context.snapshot()
        if not usage.needs_compaction or self._context_warned:
            return None
        self._context_warned = True
        return ContextWarning(
            usage=usage,
            message=(
                "上下文已占用 "
                f"{usage.effective_percent:.0f}%（{usage.context_tokens:,} / "
                f"{usage.effective_window:,} tok），达到窗口的 "
                f"{usage.warning_ratio:.0%}。\n"
                "请执行 /compact 压缩上下文；压缩成功前将暂停接受新的普通提示词。"
            ),
        )

    async def aclose(self) -> None:
        close = getattr(self._model, "aclose", None)
        if callable(close):
            await close()
        for cleaner in self._cleanup:
            try:
                outcome = cleaner()
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                # 退出阶段不阻塞、不打断其他资源的释放。
                continue

    async def _run_turn(self) -> AsyncIterator[AgentEvent]:
        round_index = 0
        while self._max_tool_rounds is None or round_index < self._max_tool_rounds:
            text_parts: list[str] = []
            pending: dict[int, _PendingToolCall] = {}
            usage: TokenUsage | None = None

            # 请求发出前先按消息历史估算一次，让 UI 在等待首 token 时也有数据。
            yield ContextStatsUpdated(self._context.refresh(self._messages))
            async for chunk in self._model.stream(
                tuple(self._messages),
                tuple(self._tools.schema()),
            ):
                if chunk.usage is not None:
                    usage = chunk.usage
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
            yield ContextStatsUpdated(
                self._context.record_request(usage, self._messages)
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
                # 只记工具名、成败与耗时；参数和结果可能含隐私，不写日志。
                logger.info(
                    "tool_call",
                    extra={
                        "tool": call.name,
                        "ok": result.ok,
                        "elapsed_ms": elapsed_ms,
                        "round": round_index,
                        "session": self._session_id,
                    },
                )
                self._messages.append(ChatMessage.tool(call.id, result.content))
                yield ToolFinished(
                    call_id=call.id,
                    name=call.name,
                    ok=result.ok,
                    content=result.content,
                    elapsed_ms=elapsed_ms,
                )
                yield ContextStatsUpdated(
                    self._context.refresh(self._messages)
                )

            round_index += 1

        message = (
            f"工具调用次数达到上限（{self._max_tool_rounds} 轮），已停止本轮操作。"
        )
        self._messages.append(ChatMessage.assistant(message))
        yield ToolRoundLimitReached(limit=self._max_tool_rounds, message=message)
        yield ContextStatsUpdated(self._context.refresh(self._messages))
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
