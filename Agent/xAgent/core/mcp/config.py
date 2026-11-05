"""``.mcp.json`` 解析与校验。

xAgent 启动时读取当前项目目录下的 ``.mcp.json``，格式与 MCP 生态常见写法一致：

```json
{
  "mcpServers": {
    "local-demo": {
      "type": "stdio",
      "command": "python",
      "args": ["server.py"],
      "env": {"DEMO_TOKEN": "${DEMO_TOKEN}"},
      "cwd": "."
    },
    "remote-demo": {
      "type": "streamable_http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer ${DEMO_API_KEY}"}
    }
  }
}
```

- 支持 ``stdio`` 与 ``streamable_http`` 两种传输；
- ``type`` 省略时按 ``command`` / ``url`` 自动推断；
- 字符串值支持 ``${VAR}`` 与 ``${VAR:-默认值}`` 环境变量展开，
  便于把密钥放在环境变量里而不写进配置文件；
- 单个服务器配置错误只会记录到 ``issues``，不会影响其他服务器。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from ..errors import ConfigurationError

CONFIG_FILENAME = ".mcp.json"

Transport = Literal["stdio", "streamable_http"]

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_STREAMABLE_HTTP_TYPES = {
    "streamable_http",
    "streamablehttp",
    "streamable",
    "http",
}
_UNSUPPORTED_TYPES = {"sse", "websocket", "ws"}


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """一个 MCP Server 的连接信息。"""

    name: str
    transport: Transport
    command: str = ""
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        """用于日志和错误提示的简短连接描述（不含请求头/密钥）。"""
        if self.transport == "stdio":
            parts = [self.command, *self.args]
            return " ".join(part for part in parts if part)
        return self.url

    def __repr__(self) -> str:  # 防止 headers 中的密钥进入日志
        return (
            f"McpServerConfig(name={self.name!r}, transport={self.transport!r}, "
            f"endpoint={self.endpoint!r})"
        )


@dataclass(frozen=True, slots=True)
class McpConfig:
    """一次启动读取到的 MCP 配置。"""

    path: Path | None = None
    servers: tuple[McpServerConfig, ...] = ()
    issues: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.servers and not self.issues and self.path is None


def discover_config_path(
    project_root: Path,
    *,
    explicit: str | Path | None = None,
) -> Path | None:
    """按 显式路径 -> 环境变量 -> 项目目录 ``.mcp.json`` 的顺序查找配置。

    显式路径和环境变量指向不存在的文件时抛出 ``ConfigurationError``；
    项目目录没有 ``.mcp.json`` 时返回 ``None``（未启用 MCP，不视为错误）。
    """
    root = Path(project_root).expanduser()

    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            raise ConfigurationError(f"找不到 MCP 配置文件：{path}")
        return path.resolve()

    env_path = os.getenv("XAGENT_MCP_CONFIG", "").strip()
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            raise ConfigurationError(
                f"XAGENT_MCP_CONFIG 指向的 MCP 配置文件不存在：{path}"
            )
        return path.resolve()

    candidate = root / CONFIG_FILENAME
    if candidate.is_file():
        return candidate.resolve()
    return None


def load_mcp_config(
    project_root: str | Path,
    *,
    path: str | Path | None = None,
) -> McpConfig:
    """读取并解析 MCP 配置。

    - 找不到配置文件：返回空的 ``McpConfig``（没有 ``path``）；
    - 文件不是合法 JSON 或顶层结构错误：抛出 ``ConfigurationError``；
    - 单个服务器字段错误：跳过该服务器并写入 ``issues``，其余服务器继续加载。
    """
    root = Path(project_root).expanduser().resolve()
    config_path = discover_config_path(root, explicit=path)
    if config_path is None:
        return McpConfig()

    try:
        raw_text = config_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(f"读取 MCP 配置失败：{config_path}：{exc}") from exc

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"MCP 配置不是合法 JSON：{config_path}：{exc}"
        ) from exc

    if not isinstance(payload, Mapping):
        raise ConfigurationError(f"MCP 配置顶层必须是 JSON 对象：{config_path}")

    raw_servers = payload.get("mcpServers", payload.get("mcp_servers"))
    if raw_servers is None:
        raise ConfigurationError(
            f"MCP 配置缺少 mcpServers 对象：{config_path}"
        )
    if not isinstance(raw_servers, Mapping):
        raise ConfigurationError(
            f"MCP 配置中的 mcpServers 必须是 JSON 对象：{config_path}"
        )

    base_dir = config_path.parent
    servers: list[McpServerConfig] = []
    issues: list[str] = []
    for name, raw in raw_servers.items():
        label = name if isinstance(name, str) and name.strip() else "<未命名服务器>"
        try:
            if not isinstance(name, str) or not name.strip():
                raise ConfigurationError("服务器名称必须是非空字符串")
            if not isinstance(raw, Mapping):
                raise ConfigurationError("服务器配置必须是 JSON 对象")
            servers.append(
                _parse_server(name.strip(), raw, base_dir=base_dir)
            )
        except ConfigurationError as exc:
            issues.append(f"{label}：{exc}")

    return McpConfig(
        path=config_path,
        servers=tuple(servers),
        issues=tuple(issues),
    )


def parse_server_entry(
    name: str,
    raw: Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
    expand_env: bool = True,
) -> McpServerConfig:
    """校验单个服务器配置，供 ``load_mcp_config`` 与 ``createMcp`` 共用。

    ``expand_env=False`` 时只做结构与类型校验，保留 ``${VAR}`` 原样：
    写入 ``.mcp.json`` 前不能要求这些环境变量当前已存在。
    """
    if not isinstance(name, str) or not name.strip():
        raise ConfigurationError("服务器名称必须是非空字符串")
    if not isinstance(raw, Mapping):
        raise ConfigurationError("服务器配置必须是 JSON 对象")
    directory = Path(base_dir).expanduser().resolve() if base_dir else Path.cwd()
    return _parse_server(
        name.strip(),
        raw,
        base_dir=directory,
        expand_env=expand_env,
    )


def _parse_server(
    name: str,
    raw: Mapping[str, Any],
    *,
    base_dir: Path,
    expand_env: bool = True,
) -> McpServerConfig:
    transport = _normalize_transport(raw)

    if transport == "stdio":
        command = _string_field(
            raw, "command", f"{name}.command", expand=expand_env
        )
        if not command:
            raise ConfigurationError("stdio 服务器必须提供 command")
        args = _string_list(raw.get("args"), f"{name}.args", expand=expand_env)
        env = _string_map(raw.get("env"), f"{name}.env", expand=expand_env)
        cwd = _resolve_cwd(
            raw.get("cwd"),
            base_dir=base_dir,
            label=f"{name}.cwd",
            expand=expand_env,
        )
        return McpServerConfig(
            name=name,
            transport="stdio",
            command=command,
            args=args,
            env=env,
            cwd=cwd,
        )

    url = _string_field(raw, "url", f"{name}.url", expand=expand_env)
    if not url:
        raise ConfigurationError("streamable_http 服务器必须提供 url")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(
            f"url 必须是合法的 http/https 地址：{url}"
        )
    headers = _string_map(raw.get("headers"), f"{name}.headers", expand=expand_env)
    return McpServerConfig(
        name=name,
        transport="streamable_http",
        url=url,
        headers=headers,
    )


def _normalize_transport(raw: Mapping[str, Any]) -> Transport:
    value = raw.get("type", raw.get("transport", ""))
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ConfigurationError("type 必须是字符串（stdio 或 streamable_http）")
    key = value.strip().lower().replace("-", "_")
    if not key:
        if raw.get("command"):
            return "stdio"
        if raw.get("url"):
            return "streamable_http"
        raise ConfigurationError(
            "无法推断传输类型：请提供 type，或提供 command（stdio）/ url（streamable_http）"
        )
    if key == "stdio":
        return "stdio"
    if key in _STREAMABLE_HTTP_TYPES:
        return "streamable_http"
    if key in _UNSUPPORTED_TYPES:
        raise ConfigurationError(
            f"暂不支持 MCP 传输类型 {value!r}；当前仅支持 stdio 和 streamable_http"
        )
    raise ConfigurationError(
        f"未知的 MCP 传输类型 {value!r}；当前仅支持 stdio 和 streamable_http"
    )


def _expand_env(value: str, *, label: str) -> str:
    if "${" not in value:
        return value

    def replace(match: re.Match[str]) -> str:
        variable = match.group(1)
        default = match.group(2)
        current = os.environ.get(variable)
        if current is not None:
            return current
        if default is not None:
            return default
        raise ConfigurationError(
            f"{label} 引用了未设置的环境变量 ${{{variable}}}"
            f"（可写成 ${{{variable}:-默认值}}）"
        )

    return _ENV_REFERENCE.sub(replace, value)


def _string_field(
    raw: Mapping[str, Any],
    key: str,
    label: str,
    *,
    expand: bool = True,
) -> str:
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigurationError(f"{label} 必须是字符串")
    text = value.strip()
    return _expand_env(text, label=label) if expand else text


def _string_list(value: Any, label: str, *, expand: bool = True) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ConfigurationError(f"{label} 必须是字符串数组")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigurationError(f"{label}[{index}] 必须是字符串")
        items.append(
            _expand_env(item, label=f"{label}[{index}]") if expand else item
        )
    return tuple(items)


def _string_map(value: Any, label: str, *, expand: bool = True) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{label} 必须是 JSON 对象")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigurationError(f"{label} 的键必须是非空字符串")
        if not isinstance(item, str):
            raise ConfigurationError(f"{label}.{key} 必须是字符串")
        result[key] = (
            _expand_env(item, label=f"{label}.{key}") if expand else item
        )
    return result


def _resolve_cwd(
    value: Any,
    *,
    base_dir: Path,
    label: str,
    expand: bool = True,
) -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigurationError(f"{label} 必须是字符串")
    text = value.strip()
    expanded = _expand_env(text, label=label) if expand else text
    if not expanded:
        return None
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()
