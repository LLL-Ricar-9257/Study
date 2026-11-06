"""配置加载。

优先级（高 -> 低）：
1. 命令行参数（workspace / config）
2. 环境变量
3. config.ini
4. 安全默认值

所有可调参数都集中在这里：``config.ini`` 缺省时使用 dataclass 的默认值，
不会因为某一项没写就崩溃。当前目录没有 ``config.ini`` 时，启动流程会自动
生成一份带注释的完整模板（见 :data:`CONFIG_TEMPLATE`），并提示用户修改。

API Key 不会出现在任何 __repr__、日志或事件中。
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.ini"
DEFAULT_CONFIG_FILENAME = "config.ini"

_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_MAX_TOOL_ROUNDS: int | None = None
_DEFAULT_MAX_TEXT_BYTES = 5 * 1024 * 1024
_DEFAULT_MAX_CHARS = 50_000
_DEFAULT_MAX_LIST_ITEMS = 2_000
_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_SEARCH_ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"
_DEFAULT_SEARCH_TOP_K = 10
_MAX_SEARCH_TOP_K = 50
_DEFAULT_SEARCH_TIMEOUT_SECONDS = 20.0

_DEFAULT_CONTEXT_WINDOW = 128 * 1024
_DEFAULT_WARNING_RATIO = 0.8
_DEFAULT_SUMMARY_TOKENS = 5_000
_DEFAULT_ARCHIVE_DIR = ".agents/context"
_DEFAULT_SESSION_DIR = ".agents/sessions"

CONFIG_TEMPLATE = """# xAgent 配置文件（首次启动时自动生成）
# 请把 [llm] api_key 改成真实值，然后重新启动 xAgent。
# 所有选项都有内置默认值，删除或留空某一项不会导致启动失败。

[llm]
# 模型服务的 API Key（必填）。不要提交真实 Key。
api_key = sk-mock-replace-me
base_url = https://api.deepseek.com/v1
model = deepseek-chat
# 单次模型请求超时（秒）
timeout_seconds = 120

[agent]
# 留空则使用启动时的当前目录；相对路径以本配置文件所在目录为基准
workspace =
# 单轮对话的工具调用轮数上限；留空或 0 表示不限制
max_tool_rounds =
max_text_bytes = 5242880
default_max_chars = 50000
max_list_items = 2000

[context]
# 上游没有返回上下文窗口时使用的默认窗口（128k）
default_window = 131072
# 达到该比例后提示并暂停普通提示词（0.8 = 80%）
warning_ratio = 0.8
# 压缩摘要的目标 token 数
summary_tokens = 5000
# 原始上下文归档目录（相对项目根目录）
archive_dir = .agents/context
max_recall_hits = 6
max_recall_chars = 4000
max_message_transcript_chars = 12000
max_tool_transcript_chars = 6000
model_transcript_chars = 240000
display_transcript_chars = 40000

[sessions]
# 会话事件日志目录（相对项目根目录）
directory = .agents/sessions
# 退出时自动保存当前会话
save_on_exit = true
# /new 时自动保存当前会话
auto_save_on_new = true
# 历史会话列表最多显示的条数
max_list = 50
# 启动时读取的项目指令文件（相对项目根目录）
agents_file = AGENTS.md
# 每个会话首次加载时注入一次 AGENTS.md，之后全程复用
inject_agents_md = true
max_agents_md_chars = 20000

[tui]
max_tool_output_chars = 8000
max_tool_arguments_chars = 3000
# 侧栏 MCP 区域最多显示的工具数
mcp_max_tools = 12

[tools]
# 本地命令行（Runcil）
runcli_timeout_seconds = 30
runcli_max_output_chars = 10000
# 网页抓取（fetchWeb）
web_timeout_seconds = 20
web_max_download_bytes = 10485760
# 网络搜索响应体上限
search_max_response_bytes = 5242880
# 启动时查询模型元数据的超时
model_info_timeout_seconds = 10

[search]
# 百度千帆 AI 搜索 API Key，留空时只有 searchweb 不可用
baidu_qianfan_api_key =
api_url = https://qianfan.baidubce.com/v2/ai_search/web_search
top_k = 10
timeout_seconds = 20

[mcp]
# 是否连接 MCP Server（也可用 XAGENT_MCP_ENABLED 覆盖）
enabled = true
# 留空则读取项目根目录的 .mcp.json
config_path =
connect_timeout = 20
call_timeout = 60
max_result_chars = 30000
shutdown_timeout = 8
"""


class ConfigFileCreatedError(ConfigurationError):
    """当前目录没有 config.ini，已自动生成模板，需要用户修改后重启。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"当前目录没有 config.ini，已生成配置模板：{path}\n"
            "请填写 [llm] api_key 后重新启动 xAgent。"
        )


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """模型连接配置。"""

    api_key: str = field(repr=False)
    base_url: str = _DEFAULT_BASE_URL
    model: str = _DEFAULT_MODEL
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class SearchSettings:
    """百度千帆 AI 搜索配置。

    API Key 允许为空：缺少时应用仍可正常启动，只有调用 ``searchweb``
    才会返回可读的错误提示，用户补全配置后无需改代码。
    """

    api_key: str = field(default="", repr=False)
    endpoint: str = _DEFAULT_SEARCH_ENDPOINT
    top_k: int = _DEFAULT_SEARCH_TOP_K
    timeout_seconds: float = _DEFAULT_SEARCH_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not 1 <= self.top_k <= _MAX_SEARCH_TOP_K:
            raise ConfigurationError(
                f"搜索 top_k 必须在 1 到 {_MAX_SEARCH_TOP_K} 之间：{self.top_k}"
            )
        if self.timeout_seconds <= 0:
            raise ConfigurationError("搜索 timeout_seconds 必须大于 0")


@dataclass(frozen=True, slots=True)
class AgentLimits:
    """核心层运行时限额。放入配置可避免 TUI/Web 各自硬编码。

    ``max_tool_rounds`` 默认为 ``None``，表示单轮对话不限制工具调用轮数；
    只有在配置中显式写入正整数时才启用上限。
    """

    max_tool_rounds: int | None = _DEFAULT_MAX_TOOL_ROUNDS
    max_text_bytes: int = _DEFAULT_MAX_TEXT_BYTES
    default_max_chars: int = _DEFAULT_MAX_CHARS
    max_list_items: int = _DEFAULT_MAX_LIST_ITEMS

    def __post_init__(self) -> None:
        if self.max_tool_rounds is not None:
            if self.max_tool_rounds < 0:
                raise ConfigurationError(
                    "max_tool_rounds 不能为负数；留空或 0 表示不限制工具轮数"
                )
            if self.max_tool_rounds == 0:
                object.__setattr__(self, "max_tool_rounds", None)
        if self.max_text_bytes < 1 or self.default_max_chars < 1:
            raise ConfigurationError("文本读取上限必须大于 0")
        if self.max_list_items < 1:
            raise ConfigurationError("max_list_items 必须大于 0")


@dataclass(frozen=True, slots=True)
class ContextSettings:
    """上下文监控、压缩与历史检索的可调参数。"""

    default_window: int = _DEFAULT_CONTEXT_WINDOW
    warning_ratio: float = _DEFAULT_WARNING_RATIO
    summary_tokens: int = _DEFAULT_SUMMARY_TOKENS
    archive_dir: str = _DEFAULT_ARCHIVE_DIR
    max_recall_hits: int = 6
    max_recall_chars: int = 4_000
    max_message_transcript_chars: int = 12_000
    max_tool_transcript_chars: int = 6_000
    model_transcript_chars: int = 240_000
    display_transcript_chars: int = 40_000

    def __post_init__(self) -> None:
        if self.default_window < 1:
            raise ConfigurationError("context.default_window 必须大于 0")
        if not 0 < self.warning_ratio <= 1:
            raise ConfigurationError("context.warning_ratio 必须在 (0, 1] 之间")
        for name in (
            "summary_tokens",
            "max_recall_hits",
            "max_recall_chars",
            "max_message_transcript_chars",
            "max_tool_transcript_chars",
            "model_transcript_chars",
            "display_transcript_chars",
        ):
            if getattr(self, name) < 1:
                raise ConfigurationError(f"context.{name} 必须大于 0")


@dataclass(frozen=True, slots=True)
class SessionSettings:
    """会话持久化与项目指令文件配置。"""

    directory: str = _DEFAULT_SESSION_DIR
    save_on_exit: bool = True
    auto_save_on_new: bool = True
    max_list: int = 50
    agents_file: str = "AGENTS.md"
    inject_agents_md: bool = True
    max_agents_md_chars: int = 20_000

    def __post_init__(self) -> None:
        if not self.directory.strip():
            raise ConfigurationError("sessions.directory 不能为空")
        if self.max_list < 1:
            raise ConfigurationError("sessions.max_list 必须大于 0")
        if not self.agents_file.strip():
            raise ConfigurationError("sessions.agents_file 不能为空")
        if self.max_agents_md_chars < 1:
            raise ConfigurationError("sessions.max_agents_md_chars 必须大于 0")


@dataclass(frozen=True, slots=True)
class TuiSettings:
    """仅影响界面展示的限额。"""

    max_tool_output_chars: int = 8_000
    max_tool_arguments_chars: int = 3_000
    mcp_max_tools: int = 12

    def __post_init__(self) -> None:
        for name in (
            "max_tool_output_chars",
            "max_tool_arguments_chars",
            "mcp_max_tools",
        ):
            if getattr(self, name) < 1:
                raise ConfigurationError(f"tui.{name} 必须大于 0")


@dataclass(frozen=True, slots=True)
class ToolsSettings:
    """工具运行参数（超时、输出上限、下载上限）。"""

    runcli_timeout_seconds: float = 30.0
    runcli_max_output_chars: int = 10_000
    web_timeout_seconds: float = 20.0
    web_max_download_bytes: int = 10 * 1024 * 1024
    search_max_response_bytes: int = 5 * 1024 * 1024
    model_info_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        for name in ("runcli_timeout_seconds", "web_timeout_seconds", "model_info_timeout_seconds"):
            if getattr(self, name) <= 0:
                raise ConfigurationError(f"tools.{name} 必须大于 0")
        for name in (
            "runcli_max_output_chars",
            "web_max_download_bytes",
            "search_max_response_bytes",
        ):
            if getattr(self, name) < 1:
                raise ConfigurationError(f"tools.{name} 必须大于 0")


@dataclass(frozen=True, slots=True)
class McpConfigSettings:
    """``[mcp]`` 段的配置；转换成 ``core.mcp.McpSettings`` 后交给管理器。

    单独定义是为了避免 ``core.config`` 在导入期依赖 ``core.tools`` 包
    （``core.tools.__init__`` 会反向导入 ``core.config``）。
    """

    enabled: bool = True
    config_path: str | None = None
    connect_timeout: float = 20.0
    call_timeout: float = 60.0
    max_result_chars: int = 30_000
    shutdown_timeout: float = 8.0

    def __post_init__(self) -> None:
        if self.connect_timeout <= 0:
            raise ConfigurationError("mcp.connect_timeout 必须大于 0")
        if self.call_timeout <= 0:
            raise ConfigurationError("mcp.call_timeout 必须大于 0")
        if self.max_result_chars < 1:
            raise ConfigurationError("mcp.max_result_chars 必须大于 0")
        if self.shutdown_timeout <= 0:
            raise ConfigurationError("mcp.shutdown_timeout 必须大于 0")

    def to_settings(self):
        """延迟导入，避免 config 与 tools 包在导入期形成环。"""

        from .mcp.manager import McpSettings

        return McpSettings(
            enabled=self.enabled,
            config_path=self.config_path,
            connect_timeout=self.connect_timeout,
            call_timeout=self.call_timeout,
            max_result_chars=self.max_result_chars,
            shutdown_timeout=self.shutdown_timeout,
        )


@dataclass(frozen=True, slots=True)
class AppConfig:
    """一次应用启动所需的全部配置。"""

    workspace: Path
    llm: LLMSettings
    limits: AgentLimits = field(default_factory=AgentLimits)
    search: SearchSettings = field(default_factory=SearchSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    sessions: SessionSettings = field(default_factory=SessionSettings)
    tui: TuiSettings = field(default_factory=TuiSettings)
    tools: ToolsSettings = field(default_factory=ToolsSettings)
    mcp: McpConfigSettings = field(default_factory=McpConfigSettings)
    config_path: Path | None = None

    def __post_init__(self) -> None:
        workspace = self.workspace.expanduser().resolve()
        object.__setattr__(self, "workspace", workspace)
        if not workspace.exists():
            raise ConfigurationError(f"工作区不存在：{workspace}")
        if not workspace.is_dir():
            raise ConfigurationError(f"工作区不是目录：{workspace}")


def _read_config(path: Path) -> configparser.ConfigParser:
    if not path.is_file():
        raise ConfigurationError(f"找不到配置文件：{path}")

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise ConfigurationError(f"读取配置文件失败：{path}：{exc}") from exc
    return parser


def write_default_config(path: str | Path) -> Path:
    """把完整模板写入指定路径；已存在的文件不会被覆盖。"""

    target = Path(path).expanduser()
    if target.is_file():
        return target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"创建配置文件失败：{target}：{exc}") from exc
    return target.resolve()


def _discover_config_path(
    explicit: str | Path | None,
    *,
    create_missing: bool = True,
) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            raise ConfigurationError(f"找不到配置文件：{path}")
        return path.resolve()

    env_path = os.getenv("XAGENT_CONFIG", "").strip()
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_file():
            raise ConfigurationError(f"XAGENT_CONFIG 指向的文件不存在：{path}")
        return path.resolve()

    candidate = Path.cwd() / DEFAULT_CONFIG_FILENAME
    if candidate.is_file():
        return candidate.resolve()

    if not create_missing:
        return None

    # 当前目录没有配置文件：生成模板并让调用方提示用户修改后重启。
    created = write_default_config(candidate)
    raise ConfigFileCreatedError(created)


def _section(parser: configparser.ConfigParser | None, name: str) -> dict[str, str]:
    if parser is None or not parser.has_section(name):
        return {}
    return {key: value.strip() for key, value in parser.items(name)}


def _positive_int(value: str, label: str, default: int) -> int:
    if not value:
        return default
    try:
        number = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{label} 必须是整数：{value}") from exc
    if number < 1:
        raise ConfigurationError(f"{label} 必须大于 0")
    return number


def _optional_positive_int(value: str, label: str) -> int | None:
    """解析可选上限：留空、0、none/unlimited/off 都表示不限制。"""
    text = (value or "").strip()
    if not text or text.casefold() in {"0", "none", "unlimited", "off", "不限制"}:
        return None
    try:
        number = int(text)
    except ValueError as exc:
        raise ConfigurationError(f"{label} 必须是整数：{value}") from exc
    if number < 1:
        raise ConfigurationError(
            f"{label} 必须是正整数；留空或 0 表示不限制工具轮数"
        )
    return number


def _positive_float(value: str, label: str, default: float) -> float:
    if not value:
        return default
    try:
        number = float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{label} 必须是数字：{value}") from exc
    if number <= 0:
        raise ConfigurationError(f"{label} 必须大于 0")
    return number


def _ratio(value: str, label: str, default: float) -> float:
    if not value:
        return default
    try:
        number = float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{label} 必须是数字：{value}") from exc
    if not 0 < number <= 1:
        raise ConfigurationError(f"{label} 必须在 (0, 1] 之间：{value}")
    return number


def _bool(value: str, label: str, default: bool) -> bool:
    text = (value or "").strip().casefold()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "是", "开启"}:
        return True
    if text in {"0", "false", "no", "off", "否", "关闭"}:
        return False
    raise ConfigurationError(f"{label} 必须是布尔值（true/false）：{value}")


def _bounded_int(value: str, label: str, default: int, maximum: int) -> int:
    if not value:
        return default
    try:
        number = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{label} 必须是整数：{value}") from exc
    if not 1 <= number <= maximum:
        raise ConfigurationError(f"{label} 必须在 1 到 {maximum} 之间：{number}")
    return number


def _resolve_workspace(
    value: str | Path | None,
    *,
    config_path: Path | None,
) -> Path:
    if value is None:
        value = os.getenv("XAGENT_WORKSPACE", "").strip() or None
    if value is None:
        return Path.cwd().resolve()

    workspace = Path(value).expanduser()
    if not workspace.is_absolute():
        # 配置中的相对路径以配置文件所在目录为基准；
        # 命令行与环境变量的相对路径则保持相对于当前目录。
        workspace = (config_path.parent if config_path else Path.cwd()) / workspace
    workspace = workspace.resolve()
    if not workspace.exists():
        raise ConfigurationError(f"工作区不存在：{workspace}")
    if not workspace.is_dir():
        raise ConfigurationError(f"工作区不是目录：{workspace}")
    return workspace


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _load_mcp_settings(section: dict[str, str]) -> McpConfigSettings:
    raw_enabled = os.getenv("XAGENT_MCP_ENABLED", "").strip().casefold()
    if raw_enabled:
        enabled = raw_enabled not in {"0", "false", "no", "off", "disabled"}
    else:
        enabled = _bool(section.get("enabled", ""), "mcp.enabled", True)
    config_path = (
        os.getenv("XAGENT_MCP_CONFIG", "").strip()
        or section.get("config_path", "").strip()
        or None
    )
    return McpConfigSettings(
        enabled=enabled,
        config_path=config_path,
        connect_timeout=_env_float(
            "XAGENT_MCP_CONNECT_TIMEOUT",
            _positive_float(
                section.get("connect_timeout", ""), "mcp.connect_timeout", 20.0
            ),
        ),
        call_timeout=_env_float(
            "XAGENT_MCP_CALL_TIMEOUT",
            _positive_float(section.get("call_timeout", ""), "mcp.call_timeout", 60.0),
        ),
        max_result_chars=_env_int(
            "XAGENT_MCP_MAX_RESULT_CHARS",
            _positive_int(
                section.get("max_result_chars", ""), "mcp.max_result_chars", 30_000
            ),
        ),
        shutdown_timeout=_env_float(
            "XAGENT_MCP_SHUTDOWN_TIMEOUT",
            _positive_float(
                section.get("shutdown_timeout", ""), "mcp.shutdown_timeout", 8.0
            ),
        ),
    )


def _load_context_settings(section: dict[str, str]) -> ContextSettings:
    return ContextSettings(
        default_window=_positive_int(
            section.get("default_window", ""),
            "context.default_window",
            _DEFAULT_CONTEXT_WINDOW,
        ),
        warning_ratio=_ratio(
            section.get("warning_ratio", ""),
            "context.warning_ratio",
            _DEFAULT_WARNING_RATIO,
        ),
        summary_tokens=_positive_int(
            section.get("summary_tokens", ""),
            "context.summary_tokens",
            _DEFAULT_SUMMARY_TOKENS,
        ),
        archive_dir=section.get("archive_dir", "").strip() or _DEFAULT_ARCHIVE_DIR,
        max_recall_hits=_positive_int(
            section.get("max_recall_hits", ""), "context.max_recall_hits", 6
        ),
        max_recall_chars=_positive_int(
            section.get("max_recall_chars", ""), "context.max_recall_chars", 4_000
        ),
        max_message_transcript_chars=_positive_int(
            section.get("max_message_transcript_chars", ""),
            "context.max_message_transcript_chars",
            12_000,
        ),
        max_tool_transcript_chars=_positive_int(
            section.get("max_tool_transcript_chars", ""),
            "context.max_tool_transcript_chars",
            6_000,
        ),
        model_transcript_chars=_positive_int(
            section.get("model_transcript_chars", ""),
            "context.model_transcript_chars",
            240_000,
        ),
        display_transcript_chars=_positive_int(
            section.get("display_transcript_chars", ""),
            "context.display_transcript_chars",
            40_000,
        ),
    )


def _load_session_settings(section: dict[str, str]) -> SessionSettings:
    return SessionSettings(
        directory=section.get("directory", "").strip() or _DEFAULT_SESSION_DIR,
        save_on_exit=_bool(section.get("save_on_exit", ""), "sessions.save_on_exit", True),
        auto_save_on_new=_bool(
            section.get("auto_save_on_new", ""), "sessions.auto_save_on_new", True
        ),
        max_list=_positive_int(section.get("max_list", ""), "sessions.max_list", 50),
        agents_file=section.get("agents_file", "").strip() or "AGENTS.md",
        inject_agents_md=_bool(
            section.get("inject_agents_md", ""),
            "sessions.inject_agents_md",
            True,
        ),
        max_agents_md_chars=_positive_int(
            section.get("max_agents_md_chars", ""),
            "sessions.max_agents_md_chars",
            20_000,
        ),
    )


def _load_tui_settings(section: dict[str, str]) -> TuiSettings:
    return TuiSettings(
        max_tool_output_chars=_positive_int(
            section.get("max_tool_output_chars", ""),
            "tui.max_tool_output_chars",
            8_000,
        ),
        max_tool_arguments_chars=_positive_int(
            section.get("max_tool_arguments_chars", ""),
            "tui.max_tool_arguments_chars",
            3_000,
        ),
        mcp_max_tools=_positive_int(
            section.get("mcp_max_tools", ""), "tui.mcp_max_tools", 12
        ),
    )


def _load_tools_settings(section: dict[str, str]) -> ToolsSettings:
    return ToolsSettings(
        runcli_timeout_seconds=_positive_float(
            section.get("runcli_timeout_seconds", ""),
            "tools.runcli_timeout_seconds",
            30.0,
        ),
        runcli_max_output_chars=_positive_int(
            section.get("runcli_max_output_chars", ""),
            "tools.runcli_max_output_chars",
            10_000,
        ),
        web_timeout_seconds=_positive_float(
            section.get("web_timeout_seconds", ""),
            "tools.web_timeout_seconds",
            20.0,
        ),
        web_max_download_bytes=_positive_int(
            section.get("web_max_download_bytes", ""),
            "tools.web_max_download_bytes",
            10 * 1024 * 1024,
        ),
        search_max_response_bytes=_positive_int(
            section.get("search_max_response_bytes", ""),
            "tools.search_max_response_bytes",
            5 * 1024 * 1024,
        ),
        model_info_timeout_seconds=_positive_float(
            section.get("model_info_timeout_seconds", ""),
            "tools.model_info_timeout_seconds",
            10.0,
        ),
    )


def load_config(
    config_path: str | Path | None = None,
    workspace: str | Path | None = None,
) -> AppConfig:
    """加载配置并完成校验。

    Args:
        config_path: 可选配置文件路径；否则依次查找当前目录、项目根目录。
        workspace: 可选工作区路径；否则依次使用环境变量、配置文件、当前目录。

    当前目录和项目根目录都没有 ``config.ini`` 时，会在当前目录生成完整模板，
    并抛出 :class:`ConfigFileCreatedError`，由启动流程提示用户修改后重启。
    """
    path = _discover_config_path(config_path)
    parser = _read_config(path) if path else None
    llm_section = _section(parser, "llm")
    agent_section = _section(parser, "agent")
    search_section = _section(parser, "search")
    context_section = _section(parser, "context")
    sessions_section = _section(parser, "sessions")
    tui_section = _section(parser, "tui")
    tools_section = _section(parser, "tools")
    mcp_section = _section(parser, "mcp")

    api_key = (
        os.getenv("XAGENT_API_KEY", "").strip()
        or os.getenv("DEEPSEEK_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
        or llm_section.get("api_key", "")
    )
    if not api_key:
        hint = f"请在 {path} 的 [llm] 段配置 api_key" if path else (
            f"请创建 {DEFAULT_CONFIG_PATH}，或设置 XAGENT_API_KEY 环境变量"
        )
        raise ConfigurationError(f"缺少 API Key。{hint}")

    base_url = (
        os.getenv("XAGENT_BASE_URL", "").strip()
        or llm_section.get("base_url", "")
        or _DEFAULT_BASE_URL
    )
    model = (
        os.getenv("XAGENT_MODEL", "").strip()
        or llm_section.get("model", "")
        or _DEFAULT_MODEL
    )

    config_workspace = workspace
    if config_workspace is None:
        config_workspace = agent_section.get("workspace", "").strip() or None
    resolved_workspace = _resolve_workspace(config_workspace, config_path=path)

    limits = AgentLimits(
        max_tool_rounds=_optional_positive_int(
            agent_section.get("max_tool_rounds", ""),
            "max_tool_rounds",
        ),
        max_text_bytes=_positive_int(
            agent_section.get("max_text_bytes", ""),
            "max_text_bytes",
            _DEFAULT_MAX_TEXT_BYTES,
        ),
        default_max_chars=_positive_int(
            agent_section.get("default_max_chars", ""),
            "default_max_chars",
            _DEFAULT_MAX_CHARS,
        ),
        max_list_items=_positive_int(
            agent_section.get("max_list_items", ""),
            "max_list_items",
            _DEFAULT_MAX_LIST_ITEMS,
        ),
    )

    timeout_seconds = _positive_float(
        llm_section.get("timeout_seconds", ""),
        "timeout_seconds",
        _DEFAULT_TIMEOUT_SECONDS,
    )
    search_api_key = (
        os.getenv("XAGENT_BAIDU_QIANFAN_API_KEY", "").strip()
        or os.getenv("BAIDU_QIANFAN_API_KEY", "").strip()
        or search_section.get("baidu_qianfan_api_key", "")
        or search_section.get("baidu qianfan_api_key", "")
        or search_section.get("qianfan_api_key", "")
        or search_section.get("baidu_api_key", "")
        or search_section.get("api_key", "")
    )
    search_endpoint = (
        os.getenv("XAGENT_SEARCH_ENDPOINT", "").strip()
        or search_section.get("api_url", "")
        or search_section.get("endpoint", "")
        or _DEFAULT_SEARCH_ENDPOINT
    )
    search_top_k = _bounded_int(
        search_section.get("top_k", ""),
        "搜索 top_k",
        _DEFAULT_SEARCH_TOP_K,
        _MAX_SEARCH_TOP_K,
    )
    search_timeout = _positive_float(
        search_section.get("timeout_seconds", ""),
        "搜索 timeout_seconds",
        _DEFAULT_SEARCH_TIMEOUT_SECONDS,
    )

    return AppConfig(
        workspace=resolved_workspace,
        llm=LLMSettings(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
        ),
        limits=limits,
        search=SearchSettings(
            api_key=search_api_key,
            endpoint=search_endpoint,
            top_k=search_top_k,
            timeout_seconds=search_timeout,
        ),
        context=_load_context_settings(context_section),
        sessions=_load_session_settings(sessions_section),
        tui=_load_tui_settings(tui_section),
        tools=_load_tools_settings(tools_section),
        mcp=_load_mcp_settings(mcp_section),
        config_path=path,
    )


__all__ = [
    "CONFIG_TEMPLATE",
    "DEFAULT_CONFIG_FILENAME",
    "DEFAULT_CONFIG_PATH",
    "PROJECT_ROOT",
    "AgentLimits",
    "AppConfig",
    "ConfigFileCreatedError",
    "ConfigurationError",
    "ContextSettings",
    "LLMSettings",
    "McpConfigSettings",
    "SearchSettings",
    "SessionSettings",
    "ToolsSettings",
    "TuiSettings",
    "load_config",
    "write_default_config",
]
