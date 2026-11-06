"""上下文压缩相关的提示词与文本排版。

压缩只做两件事：先用一段专用 Prompt 让模型生成约 5,000 Tokens 的工作摘要，
再由 :class:`~core.agent.FileAgent` 用摘要替换历史。这里不依赖任何 UI，
方便测试，也方便以后换一个前端复用同一套流程。
"""

# 压缩 Prompt 是逐字使用的中文原文，长行不按 100 列折行。
# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Sequence

from .models import ChatMessage

SUMMARY_TARGET_TOKENS = 5_000
"""压缩摘要的目标规模。"""

COMPACTION_SYSTEM_PROMPT = """你是一名 AI Agent 的上下文管理助手。当前会话即将达到上下文窗口限制，需要将历史对话压缩为一份高质量的工作摘要，以便 Agent 在新的上下文中继续执行任务。

请认真阅读提供的历史上下文，提取对后续工作仍然有价值的信息，并生成一份约 5,000 Tokens 的 Markdown 摘要。

摘要应重点保留以下内容：

1. **用户目标与要求**：用户希望完成什么任务？有哪些必须遵守的约束、偏好和验收条件？
2. **已经完成的工作**：哪些任务已经完成？实现了哪些功能？获得了哪些已经验证的结果？
3. **当前工作状态**：Agent 正在处理什么任务？当前进展到哪一步？是否存在尚未解决的问题？
4. **关键技术信息**：保留重要的文件路径、函数名称、接口定义、配置项、执行命令、错误信息和必要的代码片段。
5. **关键决策与原因**：记录用户和 Agent 已经确定的技术方案，以及重要的设计取舍。
6. **下一步任务**：明确接下来应该执行的操作，以及需要继续验证的事项。

请遵守以下要求：

- 优先保留对后续任务有直接影响的信息，删除寒暄、重复讨论、过时的尝试和无关内容。
- 对已经验证的事实与尚未验证的推测作出明确区分。
- 不要将计划完成的任务描述为已经完成。
- 重要的文件路径、函数名称、配置名称和命令应保留准确原文。
- 对失败的尝试，只保留有助于避免重复错误的信息。
- 如果信息存在冲突，应优先保留用户最新确认的要求，并简要说明仍然存在的冲突。
- 如果某些历史细节无法在摘要中完整保留，可简要说明它们已保存到原始上下文归档中，后续可以通过 recallContext 工具重新检索。
- 使用清晰的 Markdown 结构输出摘要，不要附加解释或无关内容。

**最终目标：使一个从未阅读过原始对话的 Agent，仅凭这份摘要，就能够理解当前任务、继承关键决策，并继续开展工作。**"""

_MAX_MESSAGE_TRANSCRIPT_CHARS = 12_000
_MAX_TOOL_TRANSCRIPT_CHARS = 6_000
_MAX_MODEL_TRANSCRIPT_CHARS = 240_000
_MAX_DISPLAY_TRANSCRIPT_CHARS = 40_000

_TRUNCATED_NOTE = (
    "\n\n[记录过长：后续内容已省略。完整原文保存在本次压缩的归档文件中，"
    "可用 recallContext 工具检索。]"
)


def compaction_system_prompt(target_tokens: int = SUMMARY_TARGET_TOKENS) -> str:
    """返回把目标摘要规模替换为配置值后的压缩 Prompt。"""

    text = COMPACTION_SYSTEM_PROMPT
    if target_tokens != SUMMARY_TARGET_TOKENS:
        text = text.replace("5,000 Tokens", f"{target_tokens:,} Tokens")
    return text


def render_transcript(
    messages: Sequence[ChatMessage],
    *,
    max_message_chars: int = _MAX_MESSAGE_TRANSCRIPT_CHARS,
    max_tool_chars: int = _MAX_TOOL_TRANSCRIPT_CHARS,
    total_chars: int = _MAX_MODEL_TRANSCRIPT_CHARS,
) -> str:
    """把消息渲染成给压缩模型（或压缩面板）阅读的纯文本。

    单条消息和总长度都有上限：压缩请求本身不能再把上下文塞满。被截断的
    详细内容仍完整保存在归档文件里。
    """

    lines: list[str] = []
    for message in messages:
        if message.role == "user":
            content = (message.content or "")[:max_message_chars]
            lines.append(f"用户：{content}")
        elif message.role == "assistant":
            content = (message.content or "")[:max_message_chars]
            if message.tool_calls:
                calls = "；".join(
                    f"{call.name}({call.arguments[:500]})"
                    for call in message.tool_calls
                )
                content = f"{content}\n[工具调用] {calls}".strip()
            lines.append(f"助手：{content}")
        elif message.role == "tool":
            content = (message.content or "")[:max_tool_chars]
            lines.append(f"[工具结果] {content}")
        elif message.role == "system":
            content = (message.content or "")[:max_message_chars]
            lines.append(f"[系统提示词] {content}")

    text = "\n\n".join(lines)
    if total_chars > 0 and len(text) > total_chars:
        return text[:total_chars] + _TRUNCATED_NOTE
    return text


def render_display_transcript(
    messages: Sequence[ChatMessage],
    *,
    total_chars: int = _MAX_DISPLAY_TRANSCRIPT_CHARS,
) -> str:
    """压缩面板左侧展示的原文（比给模型的更短，避免界面卡顿）。"""

    return render_transcript(messages, total_chars=total_chars)


def build_compaction_messages(
    transcript: str,
    *,
    archive_id: str,
    created_at: str,
    target_tokens: int = SUMMARY_TARGET_TOKENS,
) -> tuple[ChatMessage, ChatMessage]:
    """构造压缩请求：只有 system Prompt 和一段历史记录，不携带任何工具。"""

    user = (
        "请压缩下面这段 Agent 会话记录。\n\n"
        f"归档编号：{archive_id}\n"
        f"归档时间：{created_at}\n"
        "如果记录因过长而被截断，说明其余内容已完整保存在上述归档中，"
        "摘要里可以提示后续用 recallContext 检索。\n\n"
        "=== 会话记录开始 ===\n"
        f"{transcript}\n"
        "=== 会话记录结束 ==="
    )
    return (
        ChatMessage.system(compaction_system_prompt(target_tokens)),
        ChatMessage.user(user),
    )


def build_archive_notice(archive_id: str, created_at: str) -> str:
    """压缩摘要末尾的归档说明。"""

    return (
        f"此前上下文已于 {created_at} 完成压缩，原始上下文保存在编号为 "
        f"{archive_id} 的归档文件中。如需查找历史信息，请使用 recallContext 工具。"
    )


def build_summary_message(summary: str, notice: str) -> ChatMessage:
    """把摘要和归档说明组合成替换后的第一条 user 消息。"""

    return ChatMessage.user(
        "以下内容是对此前对话的压缩摘要，请在后续回答中把它视为已经发生的"
        "上下文；需要更早的细节时，可以调用 recallContext 检索原始归档。\n\n"
        f"{summary.strip()}\n\n---\n{notice}"
    )


__all__ = [
    "COMPACTION_SYSTEM_PROMPT",
    "SUMMARY_TARGET_TOKENS",
    "build_archive_notice",
    "build_compaction_messages",
    "build_summary_message",
    "compaction_system_prompt",
    "render_display_transcript",
    "render_transcript",
]
