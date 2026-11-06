"""TUI 专用的展示辅助函数（不包含业务逻辑）。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from rich.text import Text

from core import ContextUsage

MAX_TOOL_OUTPUT = 8_000
MAX_TOOL_ARGUMENTS = 3_000


def configure_display_limits(
    *,
    max_tool_output: int | None = None,
    max_tool_arguments: int | None = None,
) -> None:
    """由 App 启动时按 ``[tui]`` 配置覆盖展示上限。"""

    global MAX_TOOL_OUTPUT, MAX_TOOL_ARGUMENTS
    if max_tool_output is not None and max_tool_output > 0:
        MAX_TOOL_OUTPUT = int(max_tool_output)
    if max_tool_arguments is not None and max_tool_arguments > 0:
        MAX_TOOL_ARGUMENTS = int(max_tool_arguments)

# 一些终端字体（尤其 black-body CJK 字体）会把“东亚宽度不确定”的标点
# 渲染成双宽，但 Textual 按单宽计算，从而出现边框/表格错位。
# 这里统一替换为 ASCII，保证单元格计算与实际字形宽度一致。
_AMBIGUOUS_TRANSLATION = str.maketrans(
    {
        "\u2014": "-",   # em dash
        "\u2013": "-",   # en dash
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2026": "...",
        "\u00b7": "|",
        "\u00a0": " ",
        "\u200b": "",
        "\ufeff": "",
        "\u2190": "<-",
        "\u2191": "^",
        "\u2192": "->",
        "\u2193": "v",
        "\u2713": "[v]",
        "\u2714": "[v]",
        "\u2717": "[x]",
        "\u2718": "[x]",
        "\u26a0": "[!]",
        "\ufe0f": "",
        "\u25b6": ">",
        "\u25bc": "v",
        "\u258c": "|",
        "\u2500": "-",
        "\u2502": "|",
        "\u250c": "+",
        "\u2510": "+",
        "\u2514": "+",
        "\u2518": "+",
        "\u251c": "+",
        "\u2524": "+",
        "\u252c": "+",
        "\u2534": "+",
        "\u253c": "+",
        "\u2550": "=",
        "\u2551": "|",
        "\u2554": "+",
        "\u2557": "+",
        "\u255a": "+",
        "\u255d": "+",
    }
)


def sanitize_text(text: str) -> str:
    """把容易导致终端列宽错位的字符替换为 ASCII。"""
    return (text or "").translate(_AMBIGUOUS_TRANSLATION)


def pretty_arguments(raw: str) -> str:
    """把模型给出的 JSON 参数格式化；非法 JSON 原样展示。"""
    text = (raw or "").strip()
    if not text:
        return "{}"
    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError:
        return _truncate(text, MAX_TOOL_ARGUMENTS)
    if not isinstance(value, (dict, list)):
        return _truncate(text, MAX_TOOL_ARGUMENTS)
    formatted = json.dumps(value, ensure_ascii=False, indent=2)
    return _truncate(formatted, MAX_TOOL_ARGUMENTS)


def pretty_output(text: str) -> str:
    return _truncate(text.strip() or "（无输出）", MAX_TOOL_OUTPUT)


def _truncate(text: str, limit: int) -> str:
    clean = sanitize_text(text)
    if len(clean) <= limit:
        return clean
    return f"{clean[:limit]}\n...（已截断 {len(clean) - limit} 个字符）"


def status_text(model: str, state: str) -> str:
    return f"{sanitize_text(state)} | {sanitize_text(model)}"


def compact_tokens(value: int) -> str:
    """把 token 数压缩成适合单行状态栏的短文本。"""

    if value < 1_024:
        return str(value)
    if value < 10_240:
        return f"{value / 1_024:.1f}k"
    if value < 1_048_576:
        return f"{value / 1_024:.0f}k"
    return f"{value / 1_048_576:.1f}M"


def _usage_style(usage: ContextUsage) -> str:
    if usage.needs_compaction or usage.exceeds_window:
        return "#ffb3c1"
    if usage.effective_percent >= 60:
        return "#ffeaa7"
    return "#b8e6d0"


def window_source_text(usage: ContextUsage) -> str:
    """窗口来源：上游返回或本地默认 128k。"""

    return "上游获取" if usage.window_known else "默认"


def sidebar_context_text(usage: ContextUsage) -> str:
    """侧栏里始终可见的上下文占用（大小 / 窗口 / 百分比）。"""

    return (
        "上下文\n"
        f"{compact_tokens(usage.context_tokens)} / "
        f"{compact_tokens(usage.effective_window)} tok\n"
        f"已占用 {usage.effective_percent:.0f}%（{window_source_text(usage)}）"
    )


def context_bar_text(usage: ContextUsage, width: int | None = None) -> Text:
    """对话框下方的单行上下文/缓存状态。

    第一段始终是「当前上下文大小 / 模型窗口 占比」，窗口未知时显示
    ``128k 默认``。``width`` 用于在窄终端隐藏次要信息（缓存、消息、会话）。
    """

    limit = width if width is not None else 10_000
    text = Text()
    text.append("上下文 ", style="#776b83")
    if usage.estimated:
        # 使用 ASCII 标记，避免部分 CJK 字体把 ≈ 渲染成双宽导致截断。
        text.append("~", style="#776b83")
    text.append(
        compact_tokens(usage.context_tokens),
        style=f"bold {_usage_style(usage)}",
    )
    text.append(" / ", style="#776b83")
    text.append(
        compact_tokens(usage.effective_window),
        style=f"bold {_usage_style(usage)}",
    )
    if usage.window_assumed:
        text.append(" 默认", style="#a79ab3")
    text.append(
        f"  {usage.effective_percent:.0f}%",
        style=_usage_style(usage),
    )
    if usage.exceeds_window:
        text.append(" 超出窗口", style="bold #ffb3c1")
    if usage.needs_compaction:
        text.append(
            f"  已达 {usage.warning_ratio:.0%}，请执行 /compact",
            style="bold #ffb3c1",
        )

    if limit >= 72:
        text.append("   |   缓存 ", style="#776b83")
        hit_rate = usage.cache_hit_rate
        if usage.cache_reports and hit_rate is not None:
            text.append(
                f"命中 {compact_tokens(usage.total_cached_tokens)}",
                style="#ffeaa7",
            )
            text.append(" / ", style="#776b83")
            text.append(
                f"未命中 {compact_tokens(usage.total_cache_miss_tokens)}",
                style="#cdb4db",
            )
            text.append(f" ({hit_rate * 100:.0f}%)", style="#ffeaa7")
        else:
            text.append("无数据", style="#776b83")

    if limit >= 58:
        text.append("   |   消息 ", style="#776b83")
        text.append(str(usage.messages), style="#cdb4db")

    if limit >= 104:
        text.append("   |   会话 ", style="#776b83")
        text.append(
            f"输入 {compact_tokens(usage.total_prompt_tokens)}",
            style="#cdb4db",
        )
        text.append(" / ", style="#776b83")
        text.append(
            f"输出 {compact_tokens(usage.total_completion_tokens)}",
            style="#cdb4db",
        )
    return text


def mcp_sidebar_text(
    summaries: Sequence[Any],
    *,
    pending: bool = False,
    max_tools: int = 12,
) -> str:
    """侧栏 MCP 区域：服务器连接状态与已注册工具名。"""

    servers = tuple(summaries or ())
    lines = ["MCP"]
    if pending:
        lines.append("连接中...")
        return "\n".join(lines)
    if not servers:
        lines.append("未配置服务器")
        return "\n".join(lines)

    remaining = max(1, max_tools)
    for summary in servers:
        name = str(getattr(summary, "name", "mcp"))
        connected = bool(getattr(summary, "connected", False))
        if not connected:
            error = str(getattr(summary, "error", "") or "未连接")
            error = " ".join(error.split())
            if len(error) > 60:
                error = error[:60] + "..."
            lines.append(f"{name}  失败")
            lines.append(f"  {error}")
            continue
        names = tuple(getattr(summary, "tool_names", ()) or ())
        count = int(getattr(summary, "tool_count", len(names)))
        lines.append(f"{name}  {count} tools")
        for tool_name in names:
            if remaining <= 0:
                lines.append("  ...")
                break
            lines.append(f"  {tool_name}")
            remaining -= 1
    return "\n".join(lines)


def session_info_text(
    *,
    session_id: str,
    title: str,
    message_count: int,
    path: str,
    agents_md: str = "",
) -> str:
    lines = [
        "当前会话",
        f"编号：{session_id or '（未启用会话存储）'}",
        f"标题：{title or '（未命名）'}",
        f"消息：{message_count} 条",
        f"日志：{path or '（未保存）'}",
    ]
    if agents_md:
        lines.append(f"AGENTS.md：{agents_md}")
    return "\n".join(lines)


def context_detail_text(usage: ContextUsage) -> str:
    """``/context`` 命令展示的详细上下文用量。"""

    source = "本地估算" if usage.estimated else "供应商返回"
    if usage.window_known:
        window_line = (
            f"上下文：{usage.context_tokens:,} / {usage.effective_window:,} tok"
            f"（{usage.effective_percent:.1f}%，剩余 "
            f"{usage.effective_remaining:,}；{source}；窗口来源：上游获取）"
        )
    else:
        window_line = (
            f"上下文：{usage.context_tokens:,} / {usage.effective_window:,} tok"
            f"（{usage.effective_percent:.1f}%，剩余 "
            f"{usage.effective_remaining:,}；{source}；"
            "上游未返回上下文窗口，按默认 "
            f"{compact_tokens(usage.default_window)} 计算）"
        )
    lines = [
        "上下文用量",
        window_line,
        (
            f"最近请求：输入 {usage.prompt_tokens:,} tok，输出 "
            f"{usage.completion_tokens:,} tok，推理 {usage.reasoning_tokens:,} tok"
        ),
    ]
    hit_rate = usage.cache_hit_rate
    if usage.cache_reports and hit_rate is not None:
        lines.append(
            f"缓存：最近命中 {usage.cached_tokens:,} / 未命中 "
            f"{usage.cache_miss_tokens:,} tok"
        )
        lines.append(
            f"缓存累计：命中 {usage.total_cached_tokens:,} / 未命中 "
            f"{usage.total_cache_miss_tokens:,} tok，命中率 {hit_rate * 100:.1f}%"
        )
    else:
        lines.append("缓存：模型供应商未返回 Prompt 缓存数据")
    lines.append(
        f"会话累计：{usage.requests} 次请求，输入 "
        f"{usage.total_prompt_tokens:,} tok，输出 "
        f"{usage.total_completion_tokens:,} tok"
    )
    lines.append(
        f"消息：{usage.messages} 条（system {usage.system_messages} / user "
        f"{usage.user_messages} / assistant {usage.assistant_messages} / tool "
        f"{usage.tool_messages}）"
    )
    if usage.needs_compaction:
        lines.append(
            f"警告：上下文已达窗口的 {usage.warning_ratio:.0%}"
            f"（{usage.context_tokens:,} tok），"
            "请执行 /compact；压缩成功前将暂停接受新的普通提示词。"
        )
    return "\n".join(lines)
