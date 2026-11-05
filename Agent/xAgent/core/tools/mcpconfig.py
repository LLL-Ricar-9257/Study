"""``createMcp`` 工具：把用户提供的 MCP Server 配置写入 ``.mcp.json``。

与 ``createSkill`` 一样，这里只负责「配置落盘」这一件事：

- 写入目标是 xAgent 启动时会读取的那份配置文件（``--mcp-config`` /
  ``XAGENT_MCP_CONFIG`` 指定的路径，默认是启动目录下的 ``.mcp.json``）；
- 采用合并写入：文件里已有的其他服务器、其他顶层字段都原样保留；
- 写入前用 ``core.mcp.config.parse_server_entry()`` 按运行期同一套规则校验，
  校验失败不产生任何文件改动；
- 同名服务器默认拒绝覆盖，需要用户确认后显式传 ``overwrite=true``；
- 退出码/返回值只报告服务器名、传输方式与端点，不回显 ``env`` / ``headers`` 的值。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..config import AgentLimits
from ..errors import ConfigurationError, ToolError
from ..mcp.config import CONFIG_FILENAME, McpServerConfig, parse_server_entry
from .filesystem import atomic_write, human_size

_PRIMARY_KEY = "mcpServers"
_LEGACY_KEY = "mcp_servers"
_NAME_LIMIT = 64
_MAX_SERVERS = 50
_ENDPOINT_LIMIT = 160


class McpConfigToolset:
    """``createMcp`` 的实现：校验并写入 MCP 配置文件。"""

    def __init__(
        self,
        *,
        project_root: str | Path | None = None,
        workspace_root: str | Path | None = None,
        config_path: str | Path | None = None,
        limits: AgentLimits | None = None,
    ) -> None:
        self.project_root = Path(project_root or Path.cwd()).expanduser().resolve()
        self.workspace_root = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None
            else None
        )
        self.config_path = str(config_path).strip() if config_path else None
        self.limits = limits or AgentLimits()

    # -- 工具入口 ---------------------------------------------------------

    def create_mcp(
        self,
        servers: Mapping[str, Any] | str,
        overwrite: bool = False,
        path: str | None = None,
    ) -> str:
        """把 ``servers`` 合并写入 ``.mcp.json``。"""
        target = self._resolve_target(path)
        entries, validated = self._validate_servers(servers, base_dir=target.parent)

        payload, key = self._read_payload(target)
        existing = payload.get(key)
        if existing is None:
            existing = {}
        if not isinstance(existing, Mapping):
            raise ToolError(
                f"{target} 中的 {key} 必须是 JSON 对象，"
                "当前内容无法安全合并；未改动任何文件"
            )
        existing = dict(existing)

        conflicts = [name for name in entries if name in existing]
        if conflicts and not overwrite:
            raise ToolError(
                f"{target} 中已存在 MCP 服务器：{'、'.join(conflicts)}，"
                "未改动任何文件。"
                "确认要覆盖这些服务器时，先向用户确认，再以 overwrite=true 重新调用；"
                "如只想新增，请换一个服务器名。"
            )

        merged = existing | entries
        payload[key] = merged
        try:
            text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            data = text.encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ToolError(f"无法序列化 MCP 配置：{exc}") from exc

        try:
            atomic_write(target, data)
        except OSError as exc:
            raise ToolError(f"写入 MCP 配置失败：{target}：{exc}") from exc

        self._verify(target, key, merged)

        added = [name for name in entries if name not in existing]
        updated = [name for name in entries if name in existing]
        lines = [f"已写入 MCP 配置：{target}"]
        if added:
            lines.append(
                "新增服务器："
                + "、".join(self._describe(name, validated[name]) for name in added)
            )
        if updated:
            lines.append(
                "覆盖服务器："
                + "、".join(self._describe(name, validated[name]) for name in updated)
            )
        kept = [name for name in existing if name not in entries]
        lines.append(
            "文件中其他服务器：" + ("、".join(kept) if kept else "（无）")
        )
        lines.append("当前配置的全部服务器：" + "、".join(str(name) for name in merged))
        lines.append(
            f"文件大小：{human_size(len(data))}；顶层字段：{'、'.join(payload)}"
        )
        lines.append(
            "生效方式：新配置需要重启 xAgent 或开始新会话后才会连接服务器、"
            "注册对应工具；当前会话里的工具列表不会立即刷新。"
        )
        if self._has_literal_secret(entries):
            lines.append(
                "安全提示：env / headers 中存在直接写入的值，建议改用 "
                "${ENV_VAR} 从环境变量读取，避免密钥进入配置文件与版本库。"
            )
        lines.append(
            "说明：工具结果不会回显 env / headers 的具体值；"
            "配置中的 ${VAR} / ${VAR:-默认值} 会在启动时按环境变量展开。"
        )
        return "\n".join(lines)

    def registry_handlers(self) -> dict[str, Callable[..., str]]:
        return {"createMcp": self.create_mcp}

    # -- 目标路径 ---------------------------------------------------------

    def default_config_path(self) -> Path:
        """默认写入 xAgent 启动时读取的那份配置。"""
        if self.config_path:
            candidate = Path(os.path.expandvars(self.config_path)).expanduser()
            if not candidate.is_absolute():
                candidate = self.project_root / candidate
            return candidate.resolve(strict=False)
        return (self.project_root / CONFIG_FILENAME).resolve(strict=False)

    def _resolve_target(self, path: str | None) -> Path:
        text = (path or "").strip()
        if not text:
            return self.default_config_path()
        if "\x00" in text:
            raise ToolError("path 包含空字符")

        try:
            candidate = Path(os.path.expandvars(os.path.expanduser(text)))
        except (OSError, RuntimeError) as exc:
            raise ToolError(f"无法解析 path：{text}：{exc}") from exc
        if not candidate.is_absolute():
            candidate = (self.workspace_root or self.project_root) / candidate
        resolved = candidate.resolve(strict=False)

        if resolved.suffix.lower() != ".json":
            raise ToolError(f"配置文件必须是 .json 文件：{resolved}")
        if resolved.is_dir():
            raise ToolError(f"目标是一个目录，不是文件：{resolved}")
        if not resolved.parent.is_dir():
            raise ToolError(f"目标目录不存在：{resolved.parent}")
        return resolved

    # -- 参数校验 ---------------------------------------------------------

    def _validate_servers(
        self,
        servers: Mapping[str, Any] | str,
        *,
        base_dir: Path,
    ) -> tuple[dict[str, Any], dict[str, McpServerConfig]]:
        data = self._coerce_mapping(servers)
        if not data:
            raise ToolError(
                "servers 至少需要一个服务器，"
                '例如 {"demo": {"type": "stdio", "command": "python"}}'
            )
        if len(data) > _MAX_SERVERS:
            raise ToolError(
                f"一次最多写入 {_MAX_SERVERS} 个服务器，本次收到 {len(data)} 个"
            )

        entries: dict[str, Any] = {}
        validated: dict[str, McpServerConfig] = {}
        seen: dict[str, str] = {}
        for raw_name, raw_config in data.items():
            name = self._validate_name(raw_name)
            folded = name.casefold()
            if folded in seen:
                raise ToolError(
                    f"服务器名重复：{seen[folded]} 与 {name}（大小写不同也不行）"
                )
            seen[folded] = name
            if not isinstance(raw_config, Mapping):
                raise ToolError(f"服务器 {name} 的配置必须是 JSON 对象")
            try:
                validated[name] = parse_server_entry(
                    name,
                    raw_config,
                    base_dir=base_dir,
                    expand_env=False,
                )
            except ConfigurationError as exc:
                raise ToolError(f"服务器 {name} 配置不合法：{exc}") from exc
            entries[name] = dict(raw_config)

        size = len(json.dumps(entries, ensure_ascii=False).encode("utf-8"))
        if size > self.limits.max_text_bytes:
            raise ToolError(
                f"servers 过大（{human_size(size)}），"
                f"超过 {human_size(self.limits.max_text_bytes)} 的上限"
            )
        return entries, validated

    def _coerce_mapping(self, servers: Mapping[str, Any] | str) -> dict[Any, Any]:
        if isinstance(servers, str):
            text = servers.strip()
            if not text:
                raise ToolError("servers 不能为空")
            try:
                servers = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ToolError(f"servers 不是合法 JSON：{exc}") from exc
        if not isinstance(servers, Mapping):
            raise ToolError(
                "servers 必须是「服务器名 -> 配置对象」的 JSON 对象，"
                '例如 {"demo": {"type": "stdio", "command": "python"}}'
            )
        return dict(servers)

    @staticmethod
    def _validate_name(name: Any) -> str:
        if not isinstance(name, str):
            raise ToolError("服务器名必须是字符串")
        clean = name.strip()
        if not clean:
            raise ToolError("服务器名不能为空")
        if len(clean) > _NAME_LIMIT:
            raise ToolError(f"服务器名过长（超过 {_NAME_LIMIT} 个字符）：{clean}")
        if clean in {".", ".."} or "/" in clean or "\\" in clean:
            raise ToolError(f"服务器名不能包含路径分隔符：{clean}")
        if any(ord(char) < 32 for char in clean):
            raise ToolError("服务器名包含控制字符")
        return clean

    # -- 读写 -------------------------------------------------------------

    def _read_payload(self, target: Path) -> tuple[dict[str, Any], str]:
        if not target.exists():
            return {}, _PRIMARY_KEY
        try:
            raw_text = target.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ToolError(f"读取现有 MCP 配置失败：{target}：{exc}") from exc

        try:
            payload = json.loads(raw_text) if raw_text.strip() else {}
        except json.JSONDecodeError as exc:
            raise ToolError(
                f"{target} 不是合法 JSON，未改动任何内容。"
                f"请先手工修正该文件后重试：{exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ToolError(
                f"{target} 的顶层必须是 JSON 对象，未改动任何内容"
            )
        payload = dict(payload)

        if _PRIMARY_KEY in payload:
            key = _PRIMARY_KEY
        elif _LEGACY_KEY in payload:
            key = _LEGACY_KEY
        else:
            key = _PRIMARY_KEY
        return payload, key

    def _verify(self, target: Path, key: str, expected: Mapping[str, Any]) -> None:
        """回读确认落盘结果，避免静默写入半截内容。"""
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ToolError(f"写入后回读 {target} 失败：{exc}") from exc
        servers = data.get(key) if isinstance(data, Mapping) else None
        if not isinstance(servers, Mapping) or set(expected) - set(servers):
            raise ToolError(f"写入后校验 {target} 失败：服务器列表与预期不一致")

    # -- 输出 -------------------------------------------------------------

    @staticmethod
    def _describe(name: str, server: McpServerConfig) -> str:
        endpoint = " ".join(server.endpoint.split())
        if len(endpoint) > _ENDPOINT_LIMIT:
            endpoint = endpoint[:_ENDPOINT_LIMIT] + "……"
        return f"{name}（{server.transport}：{endpoint}）"

    @staticmethod
    def _has_literal_secret(entries: Mapping[str, Any]) -> bool:
        for config in entries.values():
            for field in ("env", "headers"):
                values = config.get(field)
                if not isinstance(values, Mapping):
                    continue
                if any(
                    isinstance(value, str) and "${" not in value
                    for value in values.values()
                ):
                    return True
        return False
