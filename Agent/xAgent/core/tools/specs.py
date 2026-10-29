"""文件工具、系统时间工具、网页抓取、网络搜索与本地命令行工具的 JSON Schema 声明。

声明与实现分离：新增/调整工具描述不会影响文件操作实现。
"""

from __future__ import annotations

from .types import ToolSpec

FILE_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_current_directory",
        description="获取当前工作目录的绝对路径。",
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="get_current_datetime",
        description=(
            "获取当前系统日期、时间、星期和时区。"
            "当用户询问今天、现在、今年、闰年或当前日期时间时，必须先调用此工具。"
        ),
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="list_directory",
        description="列出指定目录下的所有文件和子目录，可查看大小和修改时间。",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目录路径，相对路径基于当前工作区；默认为当前目录 .",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "是否递归列出所有层级的子项，默认 false",
                    "default": False,
                },
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="get_file_type",
        description=(
            "获取指定文件的类型信息，包括扩展名、MIME 类型、"
            "纯文本/二进制判断、编码、大小和修改时间。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径，相对路径基于当前工作区",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="read_text_file",
        description="读取指定纯文本文件的内容。二进制文件会被拒绝，超长内容会被截断。",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径，相对路径基于当前工作区",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "最多读取的字符数，默认 50000",
                    "default": 50_000,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="create_file",
        description="在指定目录下按文件名创建新文件，可同时写入初始文本内容。默认不覆盖已有文件。",
        parameters={
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "文件所在目录，必须已存在；相对路径基于当前工作区",
                },
                "filename": {
                    "type": "string",
                    "description": "文件名，只能是一个名称，不能包含路径分隔符",
                },
                "content": {
                    "type": "string",
                    "description": "初始文本内容，默认为空",
                    "default": "",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "同名文件存在时是否覆盖，默认 false",
                    "default": False,
                },
            },
            "required": ["directory", "filename"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="edit_text_file",
        description=(
            "编辑纯文本文件。mode=overwrite 覆盖全部内容；"
            "mode=append 在末尾追加；mode=replace 把 old_text 替换为 new_text。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要编辑的文件路径，相对路径基于当前工作区",
                },
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append", "replace"],
                    "description": "编辑方式",
                },
                "content": {
                    "type": "string",
                    "description": "overwrite 或 append 时写入的内容",
                },
                "old_text": {
                    "type": "string",
                    "description": "replace 时要查找的原文本",
                },
                "new_text": {
                    "type": "string",
                    "description": "replace 时替换成的新文本",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "replace 时是否替换全部匹配，默认只替换第一处",
                    "default": False,
                },
            },
            "required": ["path", "mode"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="create_directory",
        description="创建新目录，默认会自动创建缺失的父目录。",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要创建的目录路径，相对路径基于当前工作区",
                },
                "parents": {
                    "type": "boolean",
                    "description": "是否自动创建父目录，默认 true",
                    "default": True,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="delete_directory",
        description=(
            "递归删除指定目录及其全部内容，支持非空目录。"
            "此操作不可恢复，仅在用户明确要求时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要删除的目录路径，相对路径基于当前工作区",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="delete_file",
        description="删除指定文件。此操作不可恢复，仅在用户明确要求时使用。",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要删除的文件路径，相对路径基于当前工作区",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="rename_path",
        description="重命名文件或目录，新名称与原路径位于同一父目录。",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要重命名的文件或目录路径，相对路径基于当前工作区",
                },
                "new_name": {
                    "type": "string",
                    "description": "新名称，只能是一个名称，不能包含路径分隔符",
                },
            },
            "required": ["path", "new_name"],
            "additionalProperties": False,
        },
    ),
)

WEB_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="fetchWeb",
        description=(
            "获取指定网页的主要内容并转换为 Markdown。"
            "会自动去除导航栏、广告和页脚，保留标题、段落、列表、表格及链接。"
            "当用户要求阅读、总结或分析某个网页时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要获取的网页 URL，必须以 http:// 或 https:// 开头",
                }
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    ),
)

SEARCH_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="searchweb",
        description=(
            "使用百度搜索获取互联网上的实时信息，返回搜索结果摘要、来源标题和 URL。"
            "当用户询问新闻、天气、价格、赛事、近期事件等时效性信息，"
            "或你无法确定某个最新事实时使用；需要阅读网页全文时再配合 fetchWeb。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "要搜索的关键词或问题，尽量具体，"
                        "例如“2026 年诺贝尔物理学奖 获奖者”"
                    ),
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
)

RUNCLI_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="Runcil",
        description=(
            "在本机执行已经安装的命令行程序（CLI），返回标准输出、标准错误和退出码。"
            "command 只填程序名称或完整路径，args 按顺序逐项填写参数；"
            "程序名与参数不会被拼成 shell 命令，因此不需要额外引号或转义，"
            "也不能用它执行 cd、dir、echo 等 Shell 内建命令。"
            "命令在 xAgent 的当前工作目录下、以运行 xAgent 的当前用户权限执行，"
            "无法接收交互输入，默认最长运行 30 秒。"
            "请根据退出码是否为 0 以及输出内容判断执行结果，再决定下一步操作。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "要执行的程序名称（需位于 PATH 中）或可执行文件的完整路径，"
                        "例如 python、git、ffmpeg"
                    ),
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "按顺序传给程序的参数列表，每个参数单独一项；"
                        "没有参数时省略或传 []。不要在这里放入程序名。"
                    ),
                    "default": [],
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    ),
)

ALL_TOOL_SPECS = (
    FILE_TOOL_SPECS + WEB_TOOL_SPECS + SEARCH_TOOL_SPECS + RUNCLI_TOOL_SPECS
)
