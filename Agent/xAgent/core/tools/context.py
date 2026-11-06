"""上下文统计与历史归档检索工具。

``getContextStats`` 只读取 :class:`~core.context.ContextTracker` 的线程安全
快照，回答“当前用了多少 token”“缓存命中率如何”这类问题。``recallContext``
在 :class:`~core.archive.ContextArchive` 的 JSONL 归档里做本地关键词检索，
让 Agent 在压缩后仍能找回早期细节。两个工具都不调用模型，可以在工具线程里
安全执行。
"""

from __future__ import annotations

from collections.abc import Callable

from ..archive import ContextArchive, RecallResult
from ..context import ContextTracker, ContextUsage


def _number(value: int) -> str:
    return f"{value:,}"


def _percent(value: float) -> str:
    return f"{value:.1f}%"


def format_context_report(usage: ContextUsage) -> str:
    """把快照格式化为模型可读的中文报告。"""

    source = "本地估算" if usage.estimated else "供应商返回"
    if usage.window_known:
        overflow = "；已超出上下文窗口" if usage.exceeds_window else ""
        context_line = (
            f"- 当前上下文：{_number(usage.context_tokens)} tok / "
            f"{_number(usage.context_window)}"
            f"（{_percent(usage.context_percent)}，剩余 "
            f"{_number(usage.remaining_tokens)} tok{overflow}；{source}）"
        )
    else:
        context_line = (
            f"- 当前上下文：{_number(usage.context_tokens)} tok"
            f"（上游未返回上下文窗口，按默认 "
            f"{_number(usage.default_window)} tok 估算占比 "
            f"{_percent(usage.effective_percent)}；{source}）"
        )
    lines = [
        "上下文用量",
        context_line,
        (
            f"- 最近一次请求：输入 {_number(usage.prompt_tokens)} tok，"
            f"输出 {_number(usage.completion_tokens)} tok"
        ),
    ]

    if usage.cache_reports > 0:
        hit_rate = usage.cache_hit_rate
        rate_text = _percent(hit_rate * 100.0) if hit_rate is not None else "无法计算"
        lines.append(
            f"- 最近一次缓存：命中 {_number(usage.cached_tokens)} tok，"
            f"未命中 {_number(usage.cache_miss_tokens)} tok"
        )
        lines.append(
            f"- 本次会话缓存：命中 {_number(usage.total_cached_tokens)} tok，"
            f"未命中 {_number(usage.total_cache_miss_tokens)} tok，"
            f"命中率 {rate_text}"
        )
    else:
        lines.append("- 缓存：模型供应商未返回 Prompt 缓存数据")

    if usage.reasoning_tokens:
        lines.append(f"- 最近一次推理 token：{_number(usage.reasoning_tokens)} tok")

    lines.extend(
        (
            f"- 本次会话累计：{usage.requests} 次请求，输入 "
            f"{_number(usage.total_prompt_tokens)} tok，输出 "
            f"{_number(usage.total_completion_tokens)} tok",
            (
                f"- 消息数：{usage.messages}"
                f"（system {usage.system_messages} / user {usage.user_messages}"
                f" / assistant {usage.assistant_messages}"
                f" / tool {usage.tool_messages}）"
            ),
            (
                "- 上下文窗口来自启动时读取的模型上游信息；上游未提供时按默认 "
                "128k 估算。缓存数据来自模型供应商的 usage 字段。"
            ),
        )
    )
    if usage.needs_compaction:
        lines.append(
            f"- 警告：上下文已达窗口的 {usage.warning_ratio:.0%}"
            f"（阈值 {_number(usage.warning_tokens)} tok），"
            "请让用户执行 /compact 压缩上下文。"
        )
    return "\n".join(lines)


def format_recall_report(result: RecallResult) -> str:
    """把检索结果格式化为模型可读的文本，长度受档位控制。"""

    lines = [
        "上下文归档检索",
        f"- 关键词：{result.query}",
        f"- 检索范围：{result.scanned_archives} 个归档",
        f"- 命中 {result.total_matches} 条，返回 {len(result.hits)} 条",
    ]
    if not result.hits:
        lines.append(
            "- 没有找到匹配内容。可以换一个更短或更通用的关键词，"
            "并用 id 限定归档编号。"
        )
        return "\n".join(lines)

    if result.truncated:
        lines.append(
            "- 结果已截断：只返回相关度最高的片段，可缩小关键词或指定归档编号。"
        )
    for index, hit in enumerate(result.hits, start=1):
        lines.append("")
        lines.append(
            f"[{index}] 归档 {hit.archive_id} | 第 {hit.seq} 条消息 | "
            f"role={hit.role or '未知'}"
        )
        lines.append(f"片段：{hit.snippet or '（无内容）'}")
    lines.append("")
    lines.append(
        "提示：以上是原文片段（可能被截断），引用时请以归档中的完整记录为准。"
    )
    return "\n".join(lines)


class ContextToolset:
    """``getContextStats`` 的实现，与 Agent 共享同一个跟踪器。"""

    def __init__(self, tracker: ContextTracker) -> None:
        self._tracker = tracker

    def registry_handlers(self) -> dict[str, Callable[..., str]]:
        return {"getContextStats": self.get_context_stats}

    def get_context_stats(self) -> str:
        return format_context_report(self._tracker.snapshot())


class ContextArchiveToolset:
    """``recallContext`` 的实现：在历史归档中做本地全文关键词检索。"""

    def __init__(
        self,
        archive: ContextArchive,
        *,
        max_hits: int = 6,
        max_chars: int = 4_000,
    ) -> None:
        self._archive = archive
        self._max_hits = max_hits
        self._max_chars = max_chars

    def registry_handlers(self) -> dict[str, Callable[..., str]]:
        return {"recallContext": self.recall_context}

    def recall_context(self, *, text: str, id: str | None = None) -> str:
        result = self._archive.recall(
            text,
            id,
            limit=self._max_hits,
            max_chars=self._max_chars,
        )
        return format_recall_report(result)


__all__ = [
    "ContextArchiveToolset",
    "ContextToolset",
    "format_context_report",
    "format_recall_report",
]
