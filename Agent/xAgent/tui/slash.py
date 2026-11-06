"""斜杠命令元数据。

``/`` 菜单、命令过滤、帮助文本和分支处理共用同一份声明，避免菜单里能选到
但实际不存在的命令。这里只描述“有哪些命令、怎么展示”，不执行任何业务。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SlashKind = Literal["action", "compose", "template"]


@dataclass(frozen=True, slots=True)
class SlashCommand:
    """一个斜杠命令。

    - ``action``：立即执行（例如压缩上下文、查看用量）；
    - ``compose``：把 ``/<name> `` 填回输入框，等待用户补充内容（例如润色）；
    - ``template``：把一段预置提示词填回输入框，由用户确认后发送。
    """

    name: str
    title: str
    description: str
    kind: SlashKind = "action"
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        name="new",
        title="新建会话",
        description="保存当前会话并开始一个新会话",
        aliases=("newsession",),
        keywords=("新建", "会话", "session", "重置"),
    ),
    SlashCommand(
        name="resume",
        title="恢复会话",
        description="打开历史会话列表，选择后恢复对话",
        aliases=("sessions", "history"),
        keywords=("恢复", "历史", "会话", "resume", "list"),
    ),
    SlashCommand(
        name="save",
        title="保存会话",
        description="把当前会话写入 .agents/sessions 事件日志",
        keywords=("保存", "存档", "session", "save"),
    ),
    SlashCommand(
        name="compact",
        title="压缩上下文",
        description="归档当前上下文并生成摘要，释放上下文空间",
        aliases=("summarize", "compress"),
        keywords=("压缩", "摘要", "context", "token"),
    ),
    SlashCommand(
        name="session",
        title="当前会话",
        description="显示会话编号、标题、消息数与存储路径",
        keywords=("会话", "信息", "session", "info"),
    ),
    SlashCommand(
        name="rename",
        title="重命名会话",
        description="修改当前会话在历史列表中的标题",
        kind="compose",
        keywords=("重命名", "标题", "rename", "title"),
    ),
    SlashCommand(
        name="mcp",
        title="MCP 状态",
        description="显示 MCP 服务器连接状态与可用工具",
        keywords=("mcp", "服务器", "工具", "server"),
    ),
    SlashCommand(
        name="agents",
        title="项目指令",
        description="查看 AGENTS.md 的注入状态与内容摘要",
        keywords=("agents", "指令", "项目", "agents.md"),
    ),
    SlashCommand(
        name="refine",
        title="润色提示词",
        description="改写即将发送的提示词，使其更清晰具体",
        kind="compose",
        aliases=("polish",),
        keywords=("润色", "改写", "prompt", "优化"),
    ),
    SlashCommand(
        name="review",
        title="代码审查提示词",
        description="填入代码审查提示词，聚焦正确性、安全和可维护性",
        kind="template",
        keywords=("审查", "review", "diff", "代码"),
    ),
    SlashCommand(
        name="init",
        title="生成 AGENTS.md",
        description="填入扫描项目并生成项目指令文件的提示词",
        kind="template",
        keywords=("初始化", "agents", "项目", "说明"),
    ),
    SlashCommand(
        name="context",
        title="上下文用量",
        description="token、窗口占比与缓存命中明细",
        keywords=("上下文", "缓存", "usage"),
    ),
    SlashCommand(
        name="cost",
        title="会话消耗",
        description="本次会话累计输入、输出与缓存消耗",
        aliases=("usage",),
        keywords=("消耗", "费用", "token", "缓存"),
    ),
    SlashCommand(
        name="model",
        title="模型信息",
        description="当前模型、接口地址与上下文窗口",
        keywords=("模型", "model", "窗口"),
    ),
    SlashCommand(
        name="tools",
        title="工具列表",
        description="列出模型可调用的全部 Function Calling 工具",
        keywords=("工具", "tools", "function"),
    ),
    SlashCommand(
        name="help",
        title="命令帮助",
        description="显示斜杠命令与快捷键",
        keywords=("帮助", "help"),
    ),
    SlashCommand(
        name="workspace",
        title="当前工作区",
        description="显示 xAgent 可以访问的目录",
        keywords=("工作区", "目录", "workspace"),
    ),
    SlashCommand(
        name="system",
        title="系统提示词",
        description="展开或收起本轮实际使用的系统提示词",
        keywords=("系统", "提示词", "system"),
    ),
    SlashCommand(
        name="clear",
        title="清空对话",
        description="清空界面、对话历史与用量统计（保留当前会话编号）",
        keywords=("清空", "重置", "clear"),
    ),
    SlashCommand(
        name="cancel",
        title="取消生成",
        description="停止当前正在生成或执行的回复",
        keywords=("取消", "停止", "cancel"),
    ),
    SlashCommand(
        name="quit",
        title="退出 xAgent",
        description="关闭当前会话并退出程序",
        aliases=("exit", "q"),
        keywords=("退出", "quit", "exit"),
    ),
)

_COMMAND_INDEX: dict[str, SlashCommand] = {
    token: command
    for command in SLASH_COMMANDS
    for token in (command.name, *command.aliases)
}

_REVIEW_TEMPLATE = (
    "请审查当前工作区中最近的代码改动。先用 Runcil 执行 git status、git diff "
    "获取变更，再阅读相关文件。重点检查：正确性、边界条件、错误处理、安全性、"
    "性能与可维护性。按严重程度（高/中/低）列出问题，注明文件与行号，并给出"
    "具体修复建议；如果未发现问题，请说明已检查的范围。"
)

_INIT_TEMPLATE = (
    "请为当前工作区生成或更新 AGENTS.md 项目指令文件。先浏览目录结构、README、"
    "依赖与构建/测试配置，再用 create_file 或 edit_text_file 写入。内容包含："
    "项目简介、目录结构、常用命令（安装/测试/lint）、代码风格、重要约束与注意"
    "事项。不要修改其他源码文件；如果 AGENTS.md 已存在，请保留仍然有效的内容并"
    "更新过时部分。"
)


def find_command(token: str) -> SlashCommand | None:
    """按 ``compact`` 或 ``/compact`` 查找命令（含别名）。"""

    name = (token or "").strip().lstrip("/").casefold()
    return _COMMAND_INDEX.get(name)


def _match_rank(command: SlashCommand, text: str) -> int:
    """精确名称优先，其次是名称前缀，最后才是描述 / 关键词命中。"""

    names = (command.name, *command.aliases)
    if any(name.casefold() == text for name in names):
        return 0
    if any(name.casefold().startswith(text) for name in names):
        return 1
    return 2


def filter_commands(query: str) -> tuple[SlashCommand, ...]:
    """按名称、标题、描述、别名和关键词过滤命令。"""

    text = (query or "").strip().casefold()
    if not text:
        return SLASH_COMMANDS
    matches: list[SlashCommand] = []
    for command in SLASH_COMMANDS:
        haystack = " ".join(
            (
                command.name,
                command.title,
                command.description,
                *command.aliases,
                *command.keywords,
            )
        ).casefold()
        if text in haystack:
            matches.append(command)
    matches.sort(key=lambda command: _match_rank(command, text))
    return tuple(matches)


def build_template(command: SlashCommand, argument: str) -> str | None:
    """返回 ``template`` 命令要填入输入框的提示词。"""

    focus = (argument or "").strip()
    if command.name == "review":
        if focus:
            return f"{_REVIEW_TEMPLATE}\n\n本次重点关注：{focus}"
        return _REVIEW_TEMPLATE
    if command.name == "init":
        if focus:
            return f"{_INIT_TEMPLATE}\n\n补充要求：{focus}"
        return _INIT_TEMPLATE
    return None


def help_lines() -> str:
    """生成帮助文本中的命令清单。"""

    width = max(len(f"/{command.name}") for command in SLASH_COMMANDS)
    lines = []
    for command in SLASH_COMMANDS:
        name = f"/{command.name}"
        lines.append(f"  {name:<{width}}  {command.title}：{command.description}")
    return "\n".join(lines)


__all__ = [
    "SLASH_COMMANDS",
    "SlashCommand",
    "build_template",
    "filter_commands",
    "find_command",
    "help_lines",
]
