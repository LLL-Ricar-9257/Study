"""文件工具、工作区外读取、系统时间、网页抓取、网络搜索、本地命令行、
Skill 加载/创建、MCP 配置写入以及上下文统计的 JSON Schema 声明。

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
        name="is_leap_year",
        description=(
            "判断某个年份是否为闰年。规则：能被 4 整除，且（不能被 100 整除 或 能被 "
            "400 整除）。当用户询问「某年是不是闰年」「闰年有几天」「二月有没有 29 号」"
            "时使用；不传 year 时按当前年份判断。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "year": {
                    "type": "integer",
                    "description": "要判断的年份，范围 1-9999；省略时使用当前年份",
                    "minimum": 1,
                    "maximum": 9999,
                }
            },
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

EXTERNAL_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="readExternalFile",
        description=(
            "读取工作区之外的纯文本文件（只读）。"
            "当用户明确要求查看、读取或总结工作区以外路径的文件时使用，"
            "例如桌面、下载目录、其他项目或绝对路径下的文件。"
            "path 可以是绝对路径，也可以是相对于当前工作目录的路径；"
            "普通文件工具仍然只能访问工作区，本工具不修改任何文件。"
            "只支持纯文本，会自动识别编码，内容过长时截断。"
            "不要用它读取与任务无关的私密文件，如密钥、凭据、浏览器数据。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要读取的文件路径，支持绝对路径或相对当前工作目录的路径",
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

SKILL_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="loadSkill",
        description=(
            "按需加载一个 Agent Skill 的完整说明或附件。"
            "只提供 name 时返回该 Skill 的 SKILL.md 全文；"
            "同时提供 filename 时只读取 Skill 目录内的对应附件，不重复加载 SKILL.md。"
            "filename 支持相对于该 Skill 目录的路径、相对于项目根目录的路径和绝对路径；"
            "只提供文件名时会递归搜索该 Skill 目录，匹配到多个同名文件时返回候选路径。"
            "只能在系统提示列出的 Skill 中使用；需要执行某个技能的详细步骤时先调用此工具。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill 名称，必须是系统提示中列出的技能之一",
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "可选。要加载的附件文件路径或文件名；"
                        "省略时加载 SKILL.md 全文"
                    ),
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="createSkill",
        description=(
            "根据用户要求创建一个新的 Agent Skill：在当前项目的 "
            ".agents/skills/ 下创建与 name 对应的英文子目录，并写入 SKILL.md。"
            "当用户要求创建、新增或生成一个 Skill 时调用。"
            "本工具只创建目录和 SKILL.md；若还需要 references/、scripts/ 等目录或"
            "其他辅助文件，创建成功后继续调用 create_directory、create_file 等文件工具。"
            "name 必须是能转换为小写字母、数字和连字符的英文名称；"
            "skillContext 传入 SKILL.md 的完整内容，建议包含 YAML Frontmatter 的 "
            "name、description 和正文。"
            "如果同名 Skill 已存在，工具会返回明确提示并且不覆盖原内容。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Skill 名称，用于生成子目录名；"
                        "统一采用小写字母、数字和连字符组成的英文格式，"
                        "例如 pdf-tools、write-weekly-report。"
                        "工具会把空格、下划线等规范为连字符。"
                    ),
                },
                "skillContext": {
                    "type": "string",
                    "description": (
                        "要写入 SKILL.md 的完整内容。建议以 YAML Frontmatter 开头，"
                        "包含与 name 一致的 name 和简明的 description，空行后写正文；"
                        "若省略 Frontmatter，工具会用 name 和正文首段自动补齐。"
                    ),
                },
            },
            "required": ["name", "skillContext"],
            "additionalProperties": False,
        },
    ),
)

MCP_CONFIG_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="createMcp",
        description=(
            "把用户提供的 MCP Server 配置写入 .mcp.json（默认就是 xAgent 启动时读取的"
            "那份文件，即 --mcp-config / XAGENT_MCP_CONFIG 指定的路径或启动目录下的"
            ".mcp.json）。当用户要求接入、新增或修改 MCP Server / MCP 工具时调用。"
            "servers 是「服务器名 -> 配置对象」的 JSON 对象，字段与 .mcp.json 的 "
            "mcpServers 一致：stdio 用 command / args / env / cwd，"
            "streamable_http 用 url / headers，type 可省略（按 command / url 推断）。"
            "工具按 xAgent 的解析规则先校验再原子写入，校验失败不会改动文件；"
            "默认与文件中已有的其他服务器合并，同名服务器在 overwrite=false 时拒绝覆盖，"
            "需要覆盖时先向用户确认再传 overwrite=true。"
            "密钥建议写成 ${ENV_VAR}；工具结果不回显 env / headers 的值。"
            "新配置需要重启 xAgent 或开始新会话后才会连接生效。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "servers": {
                    "type": "object",
                    "description": (
                        "服务器名 -> 配置对象 的映射，例如 "
                        '{"demo": {"type": "stdio", "command": "python",'
                        ' "args": ["server.py"]}}'
                    ),
                    "additionalProperties": {"type": "object"},
                },
                "overwrite": {
                    "type": "boolean",
                    "description": (
                        "同名服务器已存在时是否覆盖，默认 false（拒绝写入并提示）"
                    ),
                    "default": False,
                },
                "path": {
                    "type": "string",
                    "description": (
                        "可选，要写入的配置文件路径；相对路径以工作区为基准。"
                        "省略时写入 xAgent 启动时读取的 .mcp.json"
                    ),
                },
            },
            "required": ["servers"],
            "additionalProperties": False,
        },
    ),
)

CONTEXT_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="getContextStats",
        description=(
            "统计当前对话的上下文用量：当前上下文 token 数、上下文窗口占比、"
            "剩余空间、最近一次请求的输入/输出 token、Prompt 缓存命中与未命中 "
            "token、本次会话累计消耗和消息数量。用户询问“上下文用了多少”"
            "“token 消耗”“缓存命中率”或需要判断是否接近上下文上限时调用。"
            "无需参数。"
        ),
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="recallContext",
        description=(
            "在已归档的历史上下文中做本地全文关键词检索。上下文被 /compact 压缩后，"
            "如果摘要里缺少某个早期细节（文件路径、函数名、命令输出、之前的决策等），"
            "调用本工具从原始记录中找回。text 是必需的关键词，可以用空格分隔多个词；"
            "id 可选，用于只检索某一个归档编号。返回相关片段、归档编号和消息位置，"
            "结果已按相关度截断，避免再次占满上下文。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "可选，归档编号（例如 20261022-153000）。"
                        "省略时在全部历史归档中检索"
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "必需，要查找的关键词或文本内容",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    ),
)

ALL_TOOL_SPECS = (
    FILE_TOOL_SPECS
    + EXTERNAL_TOOL_SPECS
    + WEB_TOOL_SPECS
    + SEARCH_TOOL_SPECS
    + RUNCLI_TOOL_SPECS
    + SKILL_TOOL_SPECS
    + MCP_CONFIG_TOOL_SPECS
    + CONTEXT_TOOL_SPECS
)
