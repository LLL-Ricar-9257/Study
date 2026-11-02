"""读取工作区之外的文件：``readExternalFile``。

普通文件工具（``read_text_file`` 等）始终被限制在受控工作区内；本工具提供
一条显式的、只读的例外通道，只在用户明确要求读取工作区外的文件时使用。

安全说明：
- 只读：不提供写入、删除、重命名能力；
- 只支持纯文本：二进制文件、设备文件会被拒绝；
- 仍受 ``AgentLimits`` 的大小和字符数限制，避免一次读入超大文件；
- 路径由模型根据用户指令给出，因此系统提示要求模型不要主动读取与任务无关的
  私密文件（密钥、凭据、浏览器数据等）。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from ..config import AgentLimits
from ..errors import ToolError
from .filesystem import detect_encoding, human_size, looks_like_text

_DEVICE_PATH_PREFIXES = ("\\\\.\\", "//./")
_GLOBAL_ROOT_PREFIX = "\\\\?\\globalroot"


class ExternalFileToolset:
    """绑定运行时限额的 ``readExternalFile`` 实现（只读、无工作区限制）。"""

    def __init__(self, limits: AgentLimits | None = None) -> None:
        self.limits = limits or AgentLimits()

    def read_external_file(self, path: str, max_chars: int | None = None) -> str:
        """读取工作区之外的纯文本文件内容。"""
        target = self._resolve(path)
        text, encoding = self._read_text(target)

        try:
            limit = (
                self.limits.default_max_chars
                if max_chars is None
                else int(max_chars)
            )
        except (TypeError, ValueError) as exc:
            raise ToolError("max_chars 必须是整数") from exc
        limit = max(1, limit)
        truncated = len(text) > limit
        shown = text[:limit]

        try:
            size = target.stat().st_size
        except OSError as exc:
            raise ToolError(f"无法读取文件信息：{target}：{exc}") from exc

        header = [
            f"文件：{target}",
            "读取通道：工作区外只读（readExternalFile）",
            f"编码：{encoding}；大小：{human_size(size)}；字符数：{len(text)}",
            "安全提示：该工具可读取工作区之外的路径，本次仅按用户要求读取。",
            "--- 内容开始 ---",
        ]
        footer = []
        if truncated:
            footer.append(
                f"--- 内容已截断，仅显示前 {limit} 个字符（共 {len(text)} 个）---"
            )
        else:
            footer.append("--- 内容结束 ---")

        if not text:
            return "\n".join(header + ["（空文件）"] + footer)
        return "\n".join(header + [shown] + footer)

    def registry_handlers(self) -> dict[str, Callable[..., str]]:
        return {"readExternalFile": self.read_external_file}

    # -- 内部辅助 ---------------------------------------------------------

    def _resolve(self, path: object) -> Path:
        if not isinstance(path, str):
            raise ToolError("path 必须是文件路径字符串")
        text = path.strip()
        if not text:
            raise ToolError("path 不能为空")
        if "\x00" in text:
            raise ToolError("path 不能包含空字符")

        lowered = text.casefold()
        if lowered.startswith(_DEVICE_PATH_PREFIXES) or lowered.startswith(
            _GLOBAL_ROOT_PREFIX
        ):
            raise ToolError("不支持读取设备、管道或全局命名空间路径")

        candidate = Path(os.path.expandvars(os.path.expanduser(text)))
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        try:
            return candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ToolError(f"无法解析路径：{text}：{exc}") from exc

    def _read_text(self, target: Path) -> tuple[str, str]:
        if not target.exists():
            raise ToolError(f"文件不存在：{target}")
        if target.is_dir():
            raise ToolError(
                f"这是一个目录，不是文件：{target}（查看目录内容请用 list_directory）"
            )

        try:
            size = target.stat().st_size
        except OSError as exc:
            raise ToolError(f"无法读取文件信息：{target}：{exc}") from exc
        if size > self.limits.max_text_bytes:
            raise ToolError(
                f"文件过大（{human_size(size)}），"
                f"超过 {human_size(self.limits.max_text_bytes)} 的读取上限"
            )

        try:
            data = target.read_bytes()
        except OSError as exc:
            raise ToolError(f"读取文件失败：{target}：{exc}") from exc
        if not looks_like_text(data[:65536]):
            raise ToolError(
                f"不是纯文本文件：{target}"
                "（本工具只能读取纯文本，可先确认文件类型）"
            )

        encoding = detect_encoding(data)
        if encoding is None:
            raise ToolError(f"不是可识别的纯文本文件：{target}")
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError as exc:
            raise ToolError(f"解码文件失败：{target}：{exc}") from exc
