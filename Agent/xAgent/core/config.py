"""配置加载。

优先级（高 -> 低）：
1. 命令行参数（workspace / config）
2. 环境变量
3. config.ini
4. 安全默认值

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

_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_MAX_TOOL_ROUNDS = 20
_DEFAULT_MAX_TEXT_BYTES = 5 * 1024 * 1024
_DEFAULT_MAX_CHARS = 50_000
_DEFAULT_MAX_LIST_ITEMS = 2_000
_DEFAULT_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """模型连接配置。"""

    api_key: str = field(repr=False)
    base_url: str = _DEFAULT_BASE_URL
    model: str = _DEFAULT_MODEL
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class AgentLimits:
    """核心层运行时限额。放入配置可避免 TUI/Web 各自硬编码。"""

    max_tool_rounds: int = _DEFAULT_MAX_TOOL_ROUNDS
    max_text_bytes: int = _DEFAULT_MAX_TEXT_BYTES
    default_max_chars: int = _DEFAULT_MAX_CHARS
    max_list_items: int = _DEFAULT_MAX_LIST_ITEMS

    def __post_init__(self) -> None:
        if self.max_tool_rounds < 1:
            raise ConfigurationError("max_tool_rounds 必须大于等于 1")
        if self.max_text_bytes < 1 or self.default_max_chars < 1:
            raise ConfigurationError("文本读取上限必须大于 0")
        if self.max_list_items < 1:
            raise ConfigurationError("max_list_items 必须大于 0")


@dataclass(frozen=True, slots=True)
class AppConfig:
    """一次应用启动所需的全部配置。"""

    workspace: Path
    llm: LLMSettings
    limits: AgentLimits = field(default_factory=AgentLimits)
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


def _discover_config_path(explicit: str | Path | None) -> Path | None:
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

    for candidate in (DEFAULT_CONFIG_PATH, Path.cwd() / "config.ini"):
        if candidate.is_file():
            return candidate.resolve()
    return None


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


def load_config(
    config_path: str | Path | None = None,
    workspace: str | Path | None = None,
) -> AppConfig:
    """加载配置并完成校验。

    Args:
        config_path: 可选配置文件路径；否则依次查找项目根目录、当前目录。
        workspace: 可选工作区路径；否则依次使用环境变量、配置文件、当前目录。
    """
    path = _discover_config_path(config_path)
    parser = _read_config(path) if path else None
    llm_section = _section(parser, "llm")
    agent_section = _section(parser, "agent")

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
        max_tool_rounds=_positive_int(
            agent_section.get("max_tool_rounds", ""),
            "max_tool_rounds",
            _DEFAULT_MAX_TOOL_ROUNDS,
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

    return AppConfig(
        workspace=resolved_workspace,
        llm=LLMSettings(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
        ),
        limits=limits,
        config_path=path,
    )
