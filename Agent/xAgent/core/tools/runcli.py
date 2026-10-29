"""本地命令行工具：``Runcil``。

按程序名调用本机已经安装的 CLI：
- ``command`` 与 ``args`` 始终组装成参数列表，通过 ``shell=False`` 执行；
- 把 stdout、stderr 和退出码一起返回，让模型根据真实结果决定下一步；
- 使用合理的执行超时，并对超长输出截断、明确标注截断情况；
- 本阶段不实现命令白名单、黑名单或审批机制，命令以运行 xAgent 的
  当前用户权限执行，仅用于受控教学实验环境。

安全边界：参数列表 + 不经过 shell 可以避免命令字符串拼接带来的注入问题，
但本工具不会限制可执行的程序，也不隔离文件系统或权限。
"""

from __future__ import annotations

import locale
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from ..errors import ToolError

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_OUTPUT_CHARS = 10_000


def _normalize_command(command: object) -> str:
    if not isinstance(command, str):
        raise ToolError("command 必须是程序名称或完整路径字符串")
    value = command.strip()
    if not value:
        raise ToolError("command 不能为空")
    if "\x00" in value:
        raise ToolError("command 不能包含空字符")
    return value


def _normalize_arguments(args: object) -> list[str]:
    """把模型给出的参数整理成字符串列表。"""
    if args is None:
        return []
    if isinstance(args, str) or not isinstance(args, Sequence):
        raise ToolError('args 必须是字符串列表，例如 ["status", "--short"]')

    arguments: list[str] = []
    for index, value in enumerate(args):
        if isinstance(value, str):
            arguments.append(value)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolError(f"args[{index}] 必须是字符串")
        # 模型偶尔会把数字参数写成 JSON number，这里按字面量转为字符串。
        arguments.append(str(value))
    return arguments


def _decode_output(data: bytes | str | None) -> str:
    """兼容 UTF-8、Windows 本地代码页和 GB18030 的命令行输出。"""
    if not data:
        return ""
    if isinstance(data, str):
        return data

    encodings = ["utf-8-sig", "utf-8"]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.insert(0, "utf-16")
    preferred = locale.getpreferredencoding(False)
    if preferred:
        encodings.append(preferred)
    encodings.append("gb18030")

    seen: set[str] = set()
    for encoding in encodings:
        key = encoding.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _truncate(text: str, limit: int) -> tuple[str, str | None]:
    if len(text) <= limit:
        return text, None
    shown = text[:limit]
    omitted = len(text) - limit
    note = (
        f"输出过长已截断：仅保留前 {limit} 个字符，"
        f"另有 {omitted} 个字符未显示。"
    )
    return shown, note


def _format_result(
    argv: list[str],
    stdout: str,
    stderr: str,
    *,
    max_output_chars: int,
    returncode: int | None = None,
    timeout: float | None = None,
) -> str:
    lines = [f"命令：{subprocess.list2cmdline(argv)}"]
    if timeout is not None:
        lines.append(
            f"退出码：不可用（执行超时：超过 {timeout:g} 秒后进程已被终止）"
        )
        lines.append("执行状态：超时")
    else:
        lines.append(f"退出码：{returncode}")
        lines.append(
            "执行状态：成功" if returncode == 0 else "执行状态：失败（非零退出码）"
        )

    for label, text in (("标准输出", stdout), ("标准错误", stderr)):
        shown, note = _truncate(text, max_output_chars)
        header = f"{label}（共 {len(text)} 字符"
        if note:
            header += f"，仅显示前 {len(shown)} 字符"
        header += "）："
        lines.append(header)
        lines.append(shown if shown else "（空）")
        if note:
            lines.append(f"[截断提示] {note}")
    return "\n".join(lines)


class RunCliToolset:
    """绑定工作目录与执行限额的本地命令行工具。"""

    def __init__(
        self,
        workspace: Path,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: int = _DEFAULT_MAX_OUTPUT_CHARS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if max_output_chars < 1:
            raise ValueError("max_output_chars 必须大于 0")

        self.workspace = Path(workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"工作目录不存在或不是目录：{self.workspace}")
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_chars = int(max_output_chars)

    def run_cli(self, command: str, args: list[str] | None = None) -> str:
        """执行 command + args，返回 stdout、stderr 和退出码。"""
        program = _normalize_command(command)
        arguments = _normalize_arguments(args)
        argv = [program, *arguments]

        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
                cwd=str(self.workspace),
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            # 超时也能拿到部分输出，一并回传给模型。
            return _format_result(
                argv,
                _decode_output(exc.stdout).rstrip("\r\n"),
                _decode_output(exc.stderr).rstrip("\r\n"),
                max_output_chars=self.max_output_chars,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise ToolError(
                f"找不到命令行程序：{program}。请确认它已经安装并在 PATH 中，"
                "或传入可执行文件的完整路径；dir、cd、echo 等 Shell 内建命令"
                "不能直接运行。"
            ) from exc
        except PermissionError as exc:
            raise ToolError(f"没有权限执行命令行程序：{program}") from exc
        except OSError as exc:
            raise ToolError(f"无法启动命令行程序 {program}：{exc}") from exc

        return _format_result(
            argv,
            _decode_output(completed.stdout).rstrip("\r\n"),
            _decode_output(completed.stderr).rstrip("\r\n"),
            max_output_chars=self.max_output_chars,
            returncode=completed.returncode,
        )

    def registry_handlers(self) -> dict[str, Callable[..., str]]:
        """返回工具名到绑定方法的映射，供 ToolRegistry 使用。"""
        return {"Runcil": self.run_cli}
