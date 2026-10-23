"""文件系统工具实现。

这里只包含业务能力，不依赖 UI、不 print、不读取全局工作区。
每个实例绑定一个 Workspace，便于测试和多工作区并行。
"""

from __future__ import annotations

import mimetypes
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..config import AgentLimits
from ..errors import ToolError
from .paths import Workspace, validate_name

_MAGIC_SIGNATURES = (
    (b"%PDF-", "PDF 文档"),
    (b"\x89PNG\r\n\x1a\n", "PNG 图片"),
    (b"\xff\xd8\xff", "JPEG 图片"),
    (b"GIF87a", "GIF 图片"),
    (b"GIF89a", "GIF 图片"),
    (b"BM", "BMP 图片"),
    (b"Rar!\x1a\x07", "RAR 压缩包"),
    (b"7z\xbc\xaf\x27\x1c", "7z 压缩包"),
    (b"PK\x03\x04", "ZIP 压缩包或 Office 文档"),
    (b"\x1f\x8b", "GZIP 压缩包"),
    (b"MZ", "Windows 可执行文件"),
    (b"\x7fELF", "Linux 可执行文件"),
    (b"SQLite format 3\x00", "SQLite 数据库"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "旧版 Office 文档"),
)

_OFFICE_SUFFIXES = {".docx", ".xlsx", ".pptx", ".docm", ".xlsm"}


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return "未知"


def detect_encoding(data: bytes) -> str | None:
    """识别纯文本编码；无法识别时返回 None。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"

    for encoding in ("utf-8", "gb18030"):
        try:
            data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return encoding
    return None


def looks_like_text(sample: bytes) -> bool:
    """根据 BOM、空字节和控制字符比例判断样本是否像纯文本。"""
    if not sample:
        return True
    if sample.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        return True
    if b"\x00" in sample:
        return False

    text = ""
    for encoding in ("utf-8", "gb18030"):
        try:
            text = sample.decode(encoding)
        except UnicodeDecodeError:
            text = sample.decode(encoding, errors="ignore")
        if text:
            break
    if not text:
        return False

    allowed = {"\n", "\r", "\t", "\f", "\b"}
    controls = sum(1 for char in text if ord(char) < 32 and char not in allowed)
    return controls / max(len(text), 1) < 0.05


def magic_type(sample: bytes, suffix: str) -> str | None:
    for signature, label in _MAGIC_SIGNATURES:
        if not sample.startswith(signature):
            continue
        if signature == b"PK\x03\x04" and suffix in _OFFICE_SUFFIXES:
            return f"Office 文档（{suffix}）"
        return label
    return None


def atomic_write(path: Path, data: bytes) -> None:
    """先写临时文件再替换，避免写入中途失败破坏原文件。"""
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def encode_text(text: str, preferred: str = "utf-8") -> tuple[bytes, str]:
    """优先使用原编码，原编码无法表示新内容时改用 UTF-8。"""
    for encoding in dict.fromkeys((preferred, "utf-8")):
        try:
            return text.encode(encoding), encoding
        except UnicodeEncodeError:
            continue
    raise ToolError("文件内容无法使用当前编码或 UTF-8 保存")


class FileToolset:
    """绑定工作区的 10 个文件工具和 1 个系统时间工具。"""

    def __init__(self, workspace: str | Path, limits: AgentLimits | None = None) -> None:
        self.workspace = Workspace(workspace)
        self.limits = limits or AgentLimits()

    # -- 内部辅助 ---------------------------------------------------------

    def _read_text(self, path: Path) -> tuple[str, str]:
        if not path.exists():
            raise ToolError(f"文件不存在：{path}")
        if path.is_dir():
            raise ToolError(f"这是一个目录，不是文件：{path}")

        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ToolError(f"无法读取文件信息：{path}：{exc}") from exc
        if size > self.limits.max_text_bytes:
            raise ToolError(
                f"文件过大（{human_size(size)}），"
                f"超过 {human_size(self.limits.max_text_bytes)} 的读取上限"
            )

        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ToolError(f"读取文件失败：{path}：{exc}") from exc
        if not looks_like_text(data[:65536]):
            raise ToolError(f"不是纯文本文件：{path}（可先用 get_file_type 查看类型）")

        encoding = detect_encoding(data)
        if encoding is None:
            raise ToolError(
                f"不是可识别的纯文本文件：{path}（可先用 get_file_type 查看类型）"
            )
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError as exc:
            raise ToolError(f"解码文件失败：{path}：{exc}") from exc

    # -- 工具 1-11 --------------------------------------------------------

    def get_current_directory(self) -> str:
        """1. 获取当前工作目录。"""
        return f"当前工作目录：{self.workspace.root}"

    def get_current_datetime(self) -> str:
        """2. 获取当前系统日期、时间、星期和时区。"""
        local = datetime.now().astimezone()
        utc = local.astimezone(UTC)
        offset = local.utcoffset() or timedelta(0)
        total_minutes = int(offset.total_seconds() / 60)
        sign = "+" if total_minutes >= 0 else "-"
        hours, minutes = divmod(abs(total_minutes), 60)
        offset_text = f"UTC{sign}{hours:02d}:{minutes:02d}"
        tz_name = local.tzname() or "本地时区"
        weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

        return (
            f"当前系统日期时间：{local.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"日期：{local.strftime('%Y-%m-%d')}\n"
            f"时间：{local.strftime('%H:%M:%S')}\n"
            f"星期：{weekdays[local.weekday()]}\n"
            f"时区：{tz_name} ({offset_text})\n"
            f"UTC 时间：{utc.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def list_directory(self, path: str = ".", recursive: bool = False) -> str:
        """3. 列出指定目录下的所有文件和子目录。"""
        target = self.workspace.resolve(path)
        if not target.exists():
            raise ToolError(f"目录不存在：{target}")
        if not target.is_dir():
            raise ToolError(f"不是目录：{target}")

        try:
            entries = sorted(
                target.rglob("*") if recursive else target.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.lower()),
            )
        except OSError as exc:
            raise ToolError(f"读取目录失败：{exc}") from exc

        directories = sum(1 for item in entries if item.is_dir())
        files = len(entries) - directories

        lines = [
            f"目录：{target}",
            f"条目：共 {len(entries)} 项（目录 {directories}，文件 {files}）"
            + ("，递归列出" if recursive else "，仅当前层"),
        ]

        for item in entries[: self.limits.max_list_items]:
            try:
                suffix = "/" if item.is_dir() else ""
                label = str(item.relative_to(target)) if recursive else item.name
                if item.is_symlink():
                    kind = "目录链接" if item.is_dir() else "文件链接"
                    info = f"[{kind}] {label}{suffix}"
                elif item.is_dir():
                    info = f"[目录] {label}/"
                else:
                    info = (
                        f"[文件] {label}  "
                        f"({human_size(item.stat().st_size)}, {mtime(item)})"
                    )
                lines.append(info)
            except OSError as exc:
                lines.append(f"[无法读取] {item.name}：{exc}")

        if len(entries) > self.limits.max_list_items:
            lines.append(f"... 其余 {len(entries) - self.limits.max_list_items} 项未显示")
        if not entries:
            lines.append("（目录为空）")

        return "\n".join(lines)

    def get_file_type(self, path: str) -> str:
        """4. 获取指定文件的类型信息。"""
        target = self.workspace.resolve(path)
        if not target.exists():
            raise ToolError(f"文件不存在：{target}")

        if target.is_dir():
            return f"路径：{target}\n类型：目录\n修改时间：{mtime(target)}"

        size = target.stat().st_size
        suffix = target.suffix.lower() or "（无扩展名）"
        mime = mimetypes.guess_type(target.name)[0] or "未知"
        try:
            with target.open("rb") as handle:
                sample = handle.read(65536)
        except OSError as exc:
            raise ToolError(f"读取文件失败：{target}：{exc}") from exc

        magic = magic_type(sample, suffix)
        is_text = looks_like_text(sample)

        if is_text:
            encoding = detect_encoding(sample) or "未知（样本可能被截断）"
            content_type = f"纯文本（编码：{encoding}）"
        else:
            content_type = "二进制"
            if magic:
                content_type += f"（{magic}）"

        lines = [
            f"文件：{target}",
            f"扩展名：{suffix}",
            f"MIME 类型：{mime}",
            f"内容类型：{content_type}",
            f"文件大小：{human_size(size)}",
            f"修改时间：{mtime(target)}",
        ]
        if magic and is_text:
            lines.append(f"文件标识：{magic}")
        return "\n".join(lines)

    def read_text_file(self, path: str, max_chars: int | None = None) -> str:
        """5. 读取指定纯文本文件的内容。"""
        target = self.workspace.resolve(path)
        text, encoding = self._read_text(target)
        size = target.stat().st_size

        limit = self.limits.default_max_chars if max_chars is None else int(max_chars)
        limit = max(1, limit)
        truncated = len(text) > limit
        shown = text[:limit]

        header = [
            f"文件：{target}",
            f"编码：{encoding}；大小：{human_size(size)}；字符数：{len(text)}",
            "--- 内容开始 ---",
        ]
        footer = []
        if truncated:
            footer.append(f"--- 内容已截断，仅显示前 {limit} 个字符（共 {len(text)} 个）---")
        else:
            footer.append("--- 内容结束 ---")

        if not text:
            return "\n".join(header + ["（空文件）"] + footer)
        return "\n".join(header + [shown] + footer)

    def create_file(
        self,
        directory: str,
        filename: str,
        content: str = "",
        overwrite: bool = False,
    ) -> str:
        """6. 根据指定的目录和文件名创建新文件。"""
        folder = self.workspace.resolve(directory)
        if not folder.exists():
            raise ToolError(f"目录不存在：{folder}（可先用 create_directory 创建）")
        if not folder.is_dir():
            raise ToolError(f"不是目录：{folder}")

        name = validate_name(filename, "文件名")
        target = self.workspace.resolve(str(folder / name))

        existed = target.exists()
        if existed:
            if target.is_dir():
                raise ToolError(f"同名目录已存在：{target}")
            if not overwrite:
                raise ToolError(f"文件已存在：{target}（如需覆盖，请设置 overwrite=true）")

        if content is None:
            content = ""
        data, encoding = encode_text(content, "utf-8")
        atomic_write(target, data)
        lines = content.count("\n") + (1 if content else 0)
        action = "已覆盖" if existed else "已创建"
        return (
            f"{action}文件：{target}\n"
            f"编码：{encoding}；大小：{human_size(len(data))}；行数：{lines}"
        )

    def edit_text_file(
        self,
        path: str,
        mode: str,
        content: str | None = None,
        old_text: str | None = None,
        new_text: str | None = None,
        replace_all: bool = False,
    ) -> str:
        """7. 编辑指定纯文本文件的内容。"""
        target = self.workspace.resolve(path)
        if not target.exists():
            raise ToolError(f"文件不存在：{target}")
        if target.is_dir():
            raise ToolError(f"这是一个目录，不是文件：{target}")

        original, encoding = self._read_text(target)
        mode = (mode or "").strip().lower()

        if mode == "overwrite":
            if content is None:
                raise ToolError("mode=overwrite 时必须提供 content")
            updated = content
        elif mode == "append":
            if content is None:
                raise ToolError("mode=append 时必须提供 content")
            updated = original + content
        elif mode == "replace":
            if not old_text:
                raise ToolError("mode=replace 时必须提供非空的 old_text")
            if new_text is None:
                raise ToolError("mode=replace 时必须提供 new_text")
            count = original.count(old_text)
            if count == 0:
                raise ToolError("在文件中没有找到 old_text，未做修改")
            updated = (
                original.replace(old_text, new_text)
                if replace_all
                else original.replace(old_text, new_text, 1)
            )
        else:
            raise ToolError("mode 必须是 overwrite、append 或 replace")

        if updated == original:
            return f"文件内容没有变化：{target}"

        data, used_encoding = encode_text(updated, encoding)
        atomic_write(target, data)

        detail = {
            "overwrite": "已覆盖全部内容",
            "append": "已追加内容",
            "replace": (
                "已替换 1 处"
                if not replace_all
                else f"已替换 {original.count(old_text or '')} 处"
            ),
        }[mode]
        return (
            f"已编辑文件：{target}\n"
            f"操作：{detail}\n"
            f"编码：{used_encoding}；大小：{human_size(len(data))}"
        )

    def create_directory(self, path: str, parents: bool = True) -> str:
        """8. 创建新目录。"""
        target = self.workspace.resolve(path)
        if target.exists():
            if target.is_dir():
                return f"目录已存在：{target}"
            raise ToolError(f"同名文件已存在，无法创建目录：{target}")

        try:
            target.mkdir(parents=parents, exist_ok=True)
        except OSError as exc:
            raise ToolError(f"创建目录失败：{exc}") from exc
        return f"已创建目录：{target}"

    def delete_directory(self, path: str) -> str:
        """9. 递归删除指定目录（支持非空目录）。"""
        target = self.workspace.resolve(path)
        if target == self.workspace.root:
            raise ToolError("不能删除当前工作区根目录")
        if not target.exists():
            raise ToolError(f"目录不存在：{target}")
        if not target.is_dir():
            raise ToolError(f"不是目录：{target}（删除文件请用 delete_file）")

        item_count = 0
        try:
            for _ in target.rglob("*"):
                item_count += 1
        except OSError:
            pass

        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise ToolError(f"删除目录失败：{exc}") from exc
        return f"已删除目录：{target}\n其中包含 {item_count} 个子项（文件/目录/链接）"

    def delete_file(self, path: str) -> str:
        """10. 删除指定文件。"""
        target = self.workspace.resolve(path)
        if not target.exists():
            raise ToolError(f"文件不存在：{target}")
        if target.is_dir():
            raise ToolError(f"这是一个目录：{target}（删除目录请用 delete_directory）")

        try:
            target.unlink()
        except OSError as exc:
            raise ToolError(f"删除文件失败：{exc}") from exc
        return f"已删除文件：{target}"

    def rename_path(self, path: str, new_name: str) -> str:
        """11. 重命名文件或目录（在同一父目录内改名）。"""
        source = self.workspace.resolve(path)
        if not source.exists():
            raise ToolError(f"路径不存在：{source}")
        if source == self.workspace.root:
            raise ToolError("不能重命名当前工作区根目录")

        name = validate_name(new_name, "新名称")
        target = source.parent / name

        if target.exists():
            try:
                same = os.path.samefile(source, target)
            except OSError:
                same = False
            # Windows 文件系统不区分大小写：仅修改大小写时需要临时名称中转。
            case_only = source.name.casefold() == target.name.casefold()
            if not same or not case_only:
                raise ToolError(f"目标名称已存在：{target}")

            if source.name != target.name:
                temporary = source.parent / f".{source.name}.rename-{os.getpid()}.tmp"
                counter = 0
                while temporary.exists():
                    counter += 1
                    temporary = source.parent / (
                        f".{source.name}.rename-{os.getpid()}-{counter}.tmp"
                    )
                try:
                    source.rename(temporary)
                    temporary.rename(target)
                except OSError as exc:
                    if temporary.exists() and not source.exists():
                        try:
                            temporary.rename(source)
                        except OSError:
                            pass
                    raise ToolError(f"重命名失败：{exc}") from exc
        else:
            try:
                source.rename(target)
            except OSError as exc:
                raise ToolError(f"重命名失败：{exc}") from exc

        kind = "目录" if target.is_dir() else "文件"
        return f"已重命名{kind}：\n原路径：{source}\n新路径：{target}"

    def registry_handlers(self) -> dict[str, Callable[..., str]]:
        """返回工具名到绑定方法的映射，供 ToolRegistry 使用。"""
        return {
            "get_current_directory": self.get_current_directory,
            "get_current_datetime": self.get_current_datetime,
            "list_directory": self.list_directory,
            "get_file_type": self.get_file_type,
            "read_text_file": self.read_text_file,
            "create_file": self.create_file,
            "edit_text_file": self.edit_text_file,
            "create_directory": self.create_directory,
            "delete_directory": self.delete_directory,
            "delete_file": self.delete_file,
            "rename_path": self.rename_path,
        }
