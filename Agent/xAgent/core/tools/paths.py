"""工作区路径策略。

所有文件操作必须经过 Workspace.resolve()，从源头隔离工作区之外的文件。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..errors import ToolError, WorkspaceBoundaryError, WorkspaceError


class Workspace:
    """受控工作区。"""

    def __init__(self, root: str | Path) -> None:
        try:
            resolved = Path(root).expanduser().resolve()
        except OSError as exc:
            raise WorkspaceError(f"无法解析工作区：{root}：{exc}") from exc
        if not resolved.exists():
            raise WorkspaceError(f"工作区不存在：{resolved}")
        if not resolved.is_dir():
            raise WorkspaceError(f"工作区不是目录：{resolved}")
        self._root = resolved

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, path: str | None) -> Path:
        """把用户路径解析为绝对路径，并校验它位于工作区内部。"""
        text = (path or ".").strip() or "."
        candidate = Path(os.path.expandvars(os.path.expanduser(text)))
        if not candidate.is_absolute():
            candidate = self._root / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise ToolError(f"无法解析路径 {text}：{exc}") from exc

        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise WorkspaceBoundaryError(
                f"路径 {resolved} 不在工作区 {self._root} 内"
            ) from exc
        return resolved


def validate_name(name: str, label: str = "名称") -> str:
    """校验单个文件/目录名，拒绝路径分隔符和 Windows 非法字符。"""
    name = (name or "").strip()
    if not name or name in {".", ".."}:
        raise ToolError(f"{label}不能为空，也不能是 . 或 ..")
    if Path(name).name != name or "/" in name or "\\" in name:
        raise ToolError(f"{label}必须是单个名称，不能包含路径分隔符：{name}")
    if any(char in name for char in '<>:"|?*'):
        raise ToolError(f"{label}包含非法字符：{name}")
    if name.endswith((" ", ".")):
        raise ToolError(f"{label}不能以空格或点结尾：{name}")
    if "\x00" in name:
        raise ToolError(f"{label}包含空字符")
    return name
