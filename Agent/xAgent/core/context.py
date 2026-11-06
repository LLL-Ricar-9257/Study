"""当前会话的上下文与缓存用量统计。

核心层不依赖任何 UI：``ContextTracker`` 在每次模型请求前后记录 token 用量，
UI 通过 ``ContextStatsUpdated`` 事件或 ``FileAgent.context_usage`` 读取快照。
供应商没有返回 ``usage`` 时，用本地估算兜底，并在快照里用 ``estimated`` 标记，
避免把估算值伪装成精确值。
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass

from .models import ChatMessage, TokenUsage

_MESSAGE_OVERHEAD = 4
_TOOL_CALL_OVERHEAD = 8
_REPLY_PRIMING = 3

DEFAULT_CONTEXT_WINDOW = 128 * 1024
"""上游没有返回上下文窗口时，界面按 128k 估算并明确标记「默认」。"""

CONTEXT_WARNING_RATIO = 0.8
"""上下文占用达到该比例时提示（并由 Agent 暂停普通对话）。"""


def estimate_text_tokens(text: str) -> int:
    """粗略估算文本 token 数。

    中文、日文、韩文和全角符号按 1 字符 1 token 计算，ASCII 按 4 字符
    1 token 计算。只用于供应商未返回 ``usage`` 时的兜底展示。
    """

    if not text:
        return 0
    wide = 0
    narrow = 0
    for char in text:
        if ord(char) >= 0x2E80:
            wide += 1
        else:
            narrow += 1
    return wide + (narrow + 3) // 4


def estimate_message_tokens(message: ChatMessage) -> int:
    """估算单条消息的 token 数（含角色和工具调用开销）。"""

    tokens = _MESSAGE_OVERHEAD + estimate_text_tokens(message.content or "")
    for call in message.tool_calls:
        tokens += _TOOL_CALL_OVERHEAD
        tokens += estimate_text_tokens(call.name)
        tokens += estimate_text_tokens(call.arguments)
    return tokens


def estimate_messages_tokens(messages: Sequence[ChatMessage]) -> int:
    """估算整段对话的 token 数。"""

    if not messages:
        return 0
    return sum(estimate_message_tokens(message) for message in messages) + _REPLY_PRIMING


def _message_counts(messages: Sequence[ChatMessage]) -> dict[str, int]:
    counts = {"system": 0, "user": 0, "assistant": 0, "tool": 0}
    for message in messages:
        if message.role in counts:
            counts[message.role] += 1
    return counts


@dataclass(frozen=True, slots=True)
class ContextUsage:
    """一次上下文用量快照，供事件、TUI 和工具共用。"""

    context_window: int = 0
    """上下文窗口；0 表示尚未从上游获取到，UI 应显示为默认值。"""

    context_tokens: int = 0
    default_window: int = DEFAULT_CONTEXT_WINDOW
    """上游未知时用于计算占比的默认窗口。"""

    warning_ratio: float = CONTEXT_WARNING_RATIO
    """触发压缩提示的占用比例。"""

    estimated: bool = True
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cached_tokens: int = 0
    total_cache_miss_tokens: int = 0
    requests: int = 0
    cache_reports: int = 0
    messages: int = 0
    system_messages: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_messages: int = 0

    @property
    def context_ratio(self) -> float:
        if self.context_window <= 0:
            return 0.0
        return self.context_tokens / self.context_window

    @property
    def context_percent(self) -> float:
        return self.context_ratio * 100.0

    @property
    def window_known(self) -> bool:
        return self.context_window > 0

    @property
    def window_assumed(self) -> bool:
        """窗口大小是否来自本地默认值（上游没有提供）。"""

        return self.context_window <= 0

    @property
    def effective_window(self) -> int:
        """实际用于计算占比的窗口：未知时回退到默认值。"""

        return self.context_window if self.window_known else self.default_window

    @property
    def effective_ratio(self) -> float:
        window = self.effective_window
        if window <= 0:
            return 0.0
        return self.context_tokens / window

    @property
    def effective_percent(self) -> float:
        return self.effective_ratio * 100.0

    @property
    def warning_tokens(self) -> int:
        """触发压缩警告的 token 阈值。"""

        return int(self.effective_window * self.warning_ratio)

    @property
    def needs_compaction(self) -> bool:
        """上下文是否已达到需要压缩的阈值。"""

        return self.effective_ratio >= self.warning_ratio

    @property
    def remaining_tokens(self) -> int:
        if not self.window_known:
            return 0
        return max(self.context_window - self.context_tokens, 0)

    @property
    def effective_remaining(self) -> int:
        """按 :attr:`effective_window` 计算的剩余空间。"""

        return max(self.effective_window - self.context_tokens, 0)

    @property
    def exceeds_window(self) -> bool:
        return self.window_known and self.context_tokens > self.context_window

    @property
    def cache_hit_rate(self) -> float | None:
        """本次会话累计的 Prompt 缓存命中率；没有缓存数据时为 ``None``。"""

        if self.cache_reports <= 0:
            return None
        total = self.total_cached_tokens + self.total_cache_miss_tokens
        if total <= 0:
            return None
        return self.total_cached_tokens / total


def _positive_int(value: int | None, fallback: int = 0) -> int:
    if value is None:
        return fallback
    return max(int(value), 0)


class ContextTracker:
    """线程安全地累计一次会话的上下文与缓存用量。

    Agent 在每次请求模型前调用 :meth:`refresh`（本地估算），在收到 ``usage``
    后调用 :meth:`record_request`（精确值），工具线程通过 :meth:`snapshot`
    读取同一份数据。
    """

    def __init__(
        self,
        context_window: int = 0,
        *,
        default_window: int = DEFAULT_CONTEXT_WINDOW,
        warning_ratio: float = CONTEXT_WARNING_RATIO,
    ) -> None:
        window = int(context_window)
        if window < 0:
            raise ValueError("context_window 不能为负数")
        if default_window < 1:
            raise ValueError("default_window 必须大于 0")
        if not 0 < warning_ratio <= 1:
            raise ValueError("warning_ratio 必须在 (0, 1] 之间")
        self._context_window = window
        self._default_window = int(default_window)
        self._warning_ratio = float(warning_ratio)
        self._lock = threading.Lock()
        self._reset_counters()

    def _reset_counters(self) -> None:
        self._context_tokens = 0
        self._estimated = True
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cached_tokens = 0
        self._cache_miss_tokens = 0
        self._reasoning_tokens = 0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_cached_tokens = 0
        self._total_cache_miss_tokens = 0
        self._requests = 0
        self._cache_reports = 0
        self._counts = {"system": 0, "user": 0, "assistant": 0, "tool": 0}

    def reset(self, messages: Sequence[ChatMessage] | None = None) -> ContextUsage:
        """清空累计数据；传入消息时同时重算当前上下文估算。"""

        with self._lock:
            self._reset_counters()
            if messages is not None:
                self._refresh_locked(messages)
            return self._snapshot_locked()

    def refresh(self, messages: Sequence[ChatMessage]) -> ContextUsage:
        """按当前消息列表重新估算上下文，并返回快照。"""

        with self._lock:
            self._refresh_locked(messages)
            return self._snapshot_locked()

    def record_request(
        self,
        usage: TokenUsage | None,
        messages: Sequence[ChatMessage],
    ) -> ContextUsage:
        """记录一次完成的模型请求。

        ``usage`` 存在时以供应商返回的 token 为准；为 ``None`` 时回退到
        :func:`estimate_messages_tokens`。此外始终更新消息数量统计。
        """

        with self._lock:
            counts = _message_counts(messages)
            estimated_tokens = estimate_messages_tokens(messages)
            self._requests += 1

            if usage is None:
                self._context_tokens = estimated_tokens
                self._estimated = True
                self._counts = counts
                return self._snapshot_locked()

            prompt = _positive_int(usage.prompt_tokens)
            completion = _positive_int(usage.completion_tokens)
            cached = (
                None
                if usage.cached_tokens is None
                else min(_positive_int(usage.cached_tokens), prompt)
            )
            miss = (
                None
                if usage.cache_miss_tokens is None
                else _positive_int(usage.cache_miss_tokens)
            )
            if cached is None and miss is not None:
                cached = max(prompt - miss, 0)
            if miss is None and cached is not None:
                miss = max(prompt - cached, 0)

            self._prompt_tokens = prompt
            self._completion_tokens = completion
            self._reasoning_tokens = _positive_int(usage.reasoning_tokens)
            self._cached_tokens = cached or 0
            self._cache_miss_tokens = miss or 0
            self._total_prompt_tokens += prompt
            self._total_completion_tokens += completion
            if cached is not None:
                self._cache_reports += 1
                self._total_cached_tokens += cached
                self._total_cache_miss_tokens += miss or 0
            self._context_tokens = prompt + completion
            self._estimated = False
            self._counts = counts
            return self._snapshot_locked()

    def snapshot(self) -> ContextUsage:
        with self._lock:
            return self._snapshot_locked()

    def _refresh_locked(self, messages: Sequence[ChatMessage]) -> None:
        self._counts = _message_counts(messages)
        self._context_tokens = estimate_messages_tokens(messages)
        self._estimated = True

    def _snapshot_locked(self) -> ContextUsage:
        counts = self._counts
        return ContextUsage(
            context_window=self._context_window,
            context_tokens=self._context_tokens,
            default_window=self._default_window,
            warning_ratio=self._warning_ratio,
            estimated=self._estimated,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            cached_tokens=self._cached_tokens,
            cache_miss_tokens=self._cache_miss_tokens,
            reasoning_tokens=self._reasoning_tokens,
            total_prompt_tokens=self._total_prompt_tokens,
            total_completion_tokens=self._total_completion_tokens,
            total_cached_tokens=self._total_cached_tokens,
            total_cache_miss_tokens=self._total_cache_miss_tokens,
            requests=self._requests,
            cache_reports=self._cache_reports,
            messages=sum(counts.values()),
            system_messages=counts["system"],
            user_messages=counts["user"],
            assistant_messages=counts["assistant"],
            tool_messages=counts["tool"],
        )


__all__ = [
    "CONTEXT_WARNING_RATIO",
    "DEFAULT_CONTEXT_WINDOW",
    "ContextTracker",
    "ContextUsage",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "estimate_text_tokens",
]
