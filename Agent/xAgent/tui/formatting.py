"""TUI 专用的展示辅助函数（不包含业务逻辑）。"""

from __future__ import annotations

import json
from typing import Any

MAX_TOOL_OUTPUT = 8_000
MAX_TOOL_ARGUMENTS = 3_000

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
