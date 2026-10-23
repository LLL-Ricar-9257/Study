"""本地文件管理对话助手。

运行：
    python chat_file_tools.py
    python chat_file_tools.py --self-test

所有文件操作都使用 Python 标准库完成，并且只允许访问启动时的工作目录
（WORKSPACE）及其子目录，避免模型误操作工作区之外的文件。
"""

from __future__ import annotations

import configparser
import json
import mimetypes
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
WORKSPACE = Path.cwd().resolve()

MAX_TEXT_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_CHARS = 50_000
MAX_LIST_ITEMS = 2_000
MAX_TOOL_ROUNDS = 20

SYSTEM_PROMPT = f"""你是一个本地文件管理助手，当前工作区是：{WORKSPACE}

请遵守以下规则：
1. 需要查看或操作文件时，必须调用工具，不要凭空编造目录内容或文件内容。
2. 相对路径都相对于当前工作区；不能访问工作区之外的路径。
3. 读取和编辑只支持纯文本文件。遇到文件类型不确定时，先调用 get_file_type。
4. 删除目录会递归删除其中所有内容；删除和重命名前要确认目标路径正确。
   只有用户明确要求时才执行破坏性操作。
5. 工具返回失败时，把失败原因告诉用户，不要假装操作成功。
6. 用中文回答，简洁说明执行结果；需要时可以汇总多个工具的结果。
"""


# ---------------------------------------------------------------------------
# 通用辅助函数
# ---------------------------------------------------------------------------


def _resolve(path: str | None) -> Path:
    """把用户路径解析为绝对路径，并确保它位于工作区内。"""
    text = (path or ".").strip()
    if not text:
        text = "."
    candidate = Path(os.path.expandvars(os.path.expanduser(text)))
    if not candidate.is_absolute():
        candidate = WORKSPACE / candidate
    resolved = candidate.resolve(strict=False)

    try:
        resolved.relative_to(WORKSPACE)
    except ValueError as exc:
        raise ValueError(f"路径 {resolved} 不在工作区 {WORKSPACE} 内") from exc
    return resolved


def _validate_name(name: str, label: str = "名称") -> str:
    """校验单个文件/目录名，拒绝路径分隔符和 Windows 非法字符。"""
    name = (name or "").strip()
    if not name or name in {".", ".."}:
        raise ValueError(f"{label}不能为空，也不能是 . 或 ..")
    if Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError(f"{label}必须是单个名称，不能包含路径分隔符：{name}")
    if any(char in name for char in '<>:"|?*'):
        raise ValueError(f"{label}包含非法字符：{name}")
    if name.endswith((" ", ".")):
        raise ValueError(f"{label}不能以空格或点结尾：{name}")
    if "\x00" in name:
        raise ValueError(f"{label}包含空字符")
    return name


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return "未知"


def _detect_encoding(data: bytes) -> str | None:
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


def _looks_like_text(sample: bytes) -> bool:
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


def _magic_type(sample: bytes, suffix: str) -> str | None:
    for signature, label in _MAGIC_SIGNATURES:
        if sample.startswith(signature):
            if signature == b"PK\x03\x04" and suffix in {
                ".docx",
                ".xlsx",
                ".pptx",
                ".docm",
                ".xlsm",
            }:
                return f"Office 文档（{suffix}）"
            return label
    return None


def _read_text(path: Path) -> tuple[str, str]:
    """读取纯文本，返回 (文本, 编码)。"""
    if not path.exists():
        raise ValueError(f"文件不存在：{path}")
    if path.is_dir():
        raise ValueError(f"这是一个目录，不是文件：{path}")

    size = path.stat().st_size
    if size > MAX_TEXT_BYTES:
        raise ValueError(
            f"文件过大（{_human_size(size)}），超过 {_human_size(MAX_TEXT_BYTES)} 的读取上限"
        )

    data = path.read_bytes()
    if not _looks_like_text(data[:65536]):
        raise ValueError(f"不是纯文本文件：{path}（可先用 get_file_type 查看类型）")

    encoding = _detect_encoding(data)
    if encoding is None:
        raise ValueError(f"不是可识别的纯文本文件：{path}（可先用 get_file_type 查看类型）")
    return data.decode(encoding), encoding


def _atomic_write(path: Path, data: bytes) -> None:
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


def _encode_text(text: str, preferred: str = "utf-8") -> tuple[bytes, str]:
    """优先使用原编码，原编码无法表示新内容时改用 UTF-8。"""
    for encoding in dict.fromkeys((preferred, "utf-8")):
        try:
            return text.encode(encoding), encoding
        except UnicodeEncodeError:
            continue
    raise ValueError("文件内容无法使用当前编码或 UTF-8 保存")


# ---------------------------------------------------------------------------
# 10 个文件操作工具
# ---------------------------------------------------------------------------


def get_current_directory() -> str:
    """1. 获取当前工作目录。"""
    return f"当前工作目录：{WORKSPACE}"


def list_directory(path: str = ".", recursive: bool = False) -> str:
    """2. 列出指定目录下的所有文件和子目录。"""
    target = _resolve(path)
    if not target.exists():
        raise ValueError(f"目录不存在：{target}")
    if not target.is_dir():
        raise ValueError(f"不是目录：{target}")

    try:
        entries = sorted(
            target.rglob("*") if recursive else target.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.lower()),
        )
    except OSError as exc:
        raise ValueError(f"读取目录失败：{exc}") from exc

    directories = sum(1 for item in entries if item.is_dir())
    files = len(entries) - directories

    lines = [
        f"目录：{target}",
        f"条目：共 {len(entries)} 项（目录 {directories}，文件 {files}）"
        + ("，递归列出" if recursive else "，仅当前层"),
    ]

    for item in entries[:MAX_LIST_ITEMS]:
        try:
            suffix = "/" if item.is_dir() else ""
            label = (
                str(item.relative_to(target))
                if recursive
                else item.name
            )
            if item.is_symlink():
                kind = "目录链接" if item.is_dir() else "文件链接"
                info = f"[{kind}] {label}{suffix}"
            elif item.is_dir():
                info = f"[目录] {label}/"
            else:
                info = f"[文件] {label}  ({_human_size(item.stat().st_size)}, {_mtime(item)})"
            lines.append(info)
        except OSError as exc:
            lines.append(f"[无法读取] {item.name}：{exc}")

    if len(entries) > MAX_LIST_ITEMS:
        lines.append(f"... 其余 {len(entries) - MAX_LIST_ITEMS} 项未显示")
    if not entries:
        lines.append("（目录为空）")

    return "\n".join(lines)


def get_file_type(path: str) -> str:
    """3. 获取指定文件的类型信息。"""
    target = _resolve(path)
    if not target.exists():
        raise ValueError(f"文件不存在：{target}")

    if target.is_dir():
        return (
            f"路径：{target}\n"
            f"类型：目录\n"
            f"修改时间：{_mtime(target)}"
        )

    size = target.stat().st_size
    suffix = target.suffix.lower() or "（无扩展名）"
    mime = mimetypes.guess_type(target.name)[0] or "未知"
    with target.open("rb") as handle:
        sample = handle.read(65536)

    magic = _magic_type(sample, suffix)
    is_text = _looks_like_text(sample)

    if is_text:
        encoding = _detect_encoding(sample) or "未知（样本可能被截断）"
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
        f"文件大小：{_human_size(size)}",
        f"修改时间：{_mtime(target)}",
    ]
    if magic and is_text:
        lines.append(f"文件标识：{magic}")
    return "\n".join(lines)


def read_text_file(path: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """4. 读取指定纯文本文件的内容。"""
    target = _resolve(path)
    text, encoding = _read_text(target)
    size = target.stat().st_size

    limit = max(1, int(max_chars))
    truncated = len(text) > limit
    shown = text[:limit]

    header = [
        f"文件：{target}",
        f"编码：{encoding}；大小：{_human_size(size)}；字符数：{len(text)}",
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
    directory: str,
    filename: str,
    content: str = "",
    overwrite: bool = False,
) -> str:
    """5. 根据指定的目录和文件名创建新文件。"""
    folder = _resolve(directory)
    if not folder.exists():
        raise ValueError(f"目录不存在：{folder}（可先用 create_directory 创建）")
    if not folder.is_dir():
        raise ValueError(f"不是目录：{folder}")

    name = _validate_name(filename, "文件名")
    target = folder / name
    target = _resolve(str(target))

    existed = target.exists()
    if existed:
        if target.is_dir():
            raise ValueError(f"同名目录已存在：{target}")
        if not overwrite:
            raise ValueError(f"文件已存在：{target}（如需覆盖，请设置 overwrite=true）")

    if content is None:
        content = ""
    data, encoding = _encode_text(content, "utf-8")
    _atomic_write(target, data)
    lines = content.count("\n") + (1 if content else 0)
    action = "已覆盖" if existed else "已创建"
    return (
        f"{action}文件：{target}\n"
        f"编码：{encoding}；大小：{_human_size(len(data))}；行数：{lines}"
    )


def edit_text_file(
    path: str,
    mode: str,
    content: str | None = None,
    old_text: str | None = None,
    new_text: str | None = None,
    replace_all: bool = False,
) -> str:
    """6. 编辑指定纯文本文件的内容。

    mode=overwrite 覆盖全部内容；mode=append 追加；mode=replace 替换文本。
    """
    target = _resolve(path)
    if not target.exists():
        raise ValueError(f"文件不存在：{target}")
    if target.is_dir():
        raise ValueError(f"这是一个目录，不是文件：{target}")

    original, encoding = _read_text(target)
    mode = (mode or "").strip().lower()

    if mode == "overwrite":
        if content is None:
            raise ValueError("mode=overwrite 时必须提供 content")
        updated = content
    elif mode == "append":
        if content is None:
            raise ValueError("mode=append 时必须提供 content")
        updated = original + content
    elif mode == "replace":
        if not old_text:
            raise ValueError("mode=replace 时必须提供非空的 old_text")
        if new_text is None:
            raise ValueError("mode=replace 时必须提供 new_text")
        count = original.count(old_text)
        if count == 0:
            raise ValueError("在文件中没有找到 old_text，未做修改")
        updated = (
            original.replace(old_text, new_text)
            if replace_all
            else original.replace(old_text, new_text, 1)
        )
    else:
        raise ValueError("mode 必须是 overwrite、append 或 replace")

    if updated == original:
        return f"文件内容没有变化：{target}"

    data, used_encoding = _encode_text(updated, encoding)
    _atomic_write(target, data)

    detail = {
        "overwrite": "已覆盖全部内容",
        "append": "已追加内容",
        "replace": "已替换 1 处" if not replace_all else f"已替换 {original.count(old_text or '')} 处",
    }[mode]
    return (
        f"已编辑文件：{target}\n"
        f"操作：{detail}\n"
        f"编码：{used_encoding}；大小：{_human_size(len(data))}"
    )


def create_directory(path: str, parents: bool = True) -> str:
    """7. 创建新目录。"""
    target = _resolve(path)
    if target.exists():
        if target.is_dir():
            return f"目录已存在：{target}"
        raise ValueError(f"同名文件已存在，无法创建目录：{target}")

    try:
        target.mkdir(parents=parents, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"创建目录失败：{exc}") from exc
    return f"已创建目录：{target}"


def delete_directory(path: str) -> str:
    """8. 递归删除指定目录（支持非空目录）。"""
    target = _resolve(path)
    if target == WORKSPACE:
        raise ValueError("不能删除当前工作区根目录")
    if not target.exists():
        raise ValueError(f"目录不存在：{target}")
    if not target.is_dir():
        raise ValueError(f"不是目录：{target}（删除文件请用 delete_file）")

    item_count = 0
    try:
        for _ in target.rglob("*"):
            item_count += 1
    except OSError:
        pass

    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise ValueError(f"删除目录失败：{exc}") from exc
    return f"已删除目录：{target}\n其中包含 {item_count} 个子项（文件/目录/链接）"


def delete_file(path: str) -> str:
    """9. 删除指定文件。"""
    target = _resolve(path)
    if not target.exists():
        raise ValueError(f"文件不存在：{target}")
    if target.is_dir():
        raise ValueError(f"这是一个目录：{target}（删除目录请用 delete_directory）")

    try:
        target.unlink()
    except OSError as exc:
        raise ValueError(f"删除文件失败：{exc}") from exc
    return f"已删除文件：{target}"


def rename_path(path: str, new_name: str) -> str:
    """10. 重命名文件或目录（在同一父目录内改名）。"""
    source = _resolve(path)
    if not source.exists():
        raise ValueError(f"路径不存在：{source}")
    if source == WORKSPACE:
        raise ValueError("不能重命名当前工作区根目录")

    name = _validate_name(new_name, "新名称")
    target = source.parent / name

    if target.exists():
        try:
            same = os.path.samefile(source, target)
        except OSError:
            same = False
        # Windows 文件系统不区分大小写：仅修改大小写时需要通过临时名称中转。
        case_only = source.name.casefold() == target.name.casefold()
        if not same or not case_only:
            raise ValueError(f"目标名称已存在：{target}")

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
                raise ValueError(f"重命名失败：{exc}") from exc
    else:
        try:
            source.rename(target)
        except OSError as exc:
            raise ValueError(f"重命名失败：{exc}") from exc

    kind = "目录" if target.is_dir() else "文件"
    return f"已重命名{kind}：\n原路径：{source}\n新路径：{target}"


TOOL_FUNCTIONS = {
    "get_current_directory": get_current_directory,
    "list_directory": list_directory,
    "get_file_type": get_file_type,
    "read_text_file": read_text_file,
    "create_file": create_file,
    "edit_text_file": edit_text_file,
    "create_directory": create_directory,
    "delete_directory": delete_directory,
    "delete_file": delete_file,
    "rename_path": rename_path,
}


# ---------------------------------------------------------------------------
# 工具声明（OpenAI function calling 格式）
# ---------------------------------------------------------------------------


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_current_directory",
            "description": "获取当前工作目录的绝对路径。",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出指定目录下的所有文件和子目录，可查看大小和修改时间。",
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_type",
            "description": "获取指定文件的类型信息，包括扩展名、MIME 类型、纯文本/二进制判断、编码、大小和修改时间。",
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_text_file",
            "description": "读取指定纯文本文件的内容。二进制文件会被拒绝，超长内容会被截断。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径，相对路径基于当前工作区",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最多读取的字符数，默认 50000",
                        "default": DEFAULT_MAX_CHARS,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "在指定目录下按文件名创建新文件，可同时写入初始文本内容。默认不覆盖已有文件。",
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_text_file",
            "description": "编辑纯文本文件。mode=overwrite 覆盖全部内容；mode=append 在末尾追加；mode=replace 把 old_text 替换为 new_text。",
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_directory",
            "description": "创建新目录，默认会自动创建缺失的父目录。",
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_directory",
            "description": "递归删除指定目录及其全部内容，支持非空目录。此操作不可恢复，仅在用户明确要求时使用。",
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除指定文件。此操作不可恢复，仅在用户明确要求时使用。",
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_path",
            "description": "重命名文件或目录，新名称与原路径位于同一父目录。",
            "parameters": {
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
        },
    },
]


# ---------------------------------------------------------------------------
# 对话与工具调用
# ---------------------------------------------------------------------------


def _load_client() -> tuple[OpenAI, str]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"找不到配置文件：{CONFIG_PATH}")

    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    if not cfg.has_section("llm"):
        raise RuntimeError(f"配置文件缺少 [llm] 段：{CONFIG_PATH}")

    api_key = cfg.get("llm", "api_key", fallback="").strip()
    base_url = cfg.get("llm", "base_url", fallback="").strip()
    model = cfg.get("llm", "model", fallback="").strip()
    if not api_key:
        raise RuntimeError("配置文件中的 api_key 为空")
    if not model:
        raise RuntimeError("配置文件中的 model 为空")

    client = OpenAI(api_key=api_key, base_url=base_url)
    return client, model


def run_tool_calls(tool_calls: list[dict[str, Any]], messages: list[dict[str, Any]]) -> None:
    for tool_call in tool_calls:
        name = tool_call.get("function", {}).get("name", "")
        function = TOOL_FUNCTIONS.get(name)
        print(f"\n[工具] {name} ... ", end="", flush=True)

        if function is None:
            content = f"未知工具：{name}"
            print("未知工具", flush=True)
        else:
            try:
                arguments = json.loads(tool_call["function"].get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("工具参数必须是 JSON 对象")
                result = function(**arguments)
                content = result if isinstance(result, str) else str(result)
                print("完成", flush=True)
            except Exception as exc:  # 工具错误要回传给模型，而不是中断对话
                content = f"工具 {name} 执行失败：{exc}"
                print(f"失败：{exc}", flush=True)

        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.get("id", ""),
                "content": content,
            }
        )


def stream_chat(
    client: OpenAI,
    model: str,
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=TOOLS,
        stream=True,
    )

    content = ""
    tool_calls: dict[int, dict[str, Any]] = {}
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            content += delta.content
            print(delta.content, end="", flush=True)

        for tool_call in delta.tool_calls or []:
            index = tool_call.index if tool_call.index is not None else 0
            entry = tool_calls.setdefault(
                index,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if tool_call.id:
                entry["id"] += tool_call.id
            if tool_call.function:
                if tool_call.function.name:
                    entry["function"]["name"] += tool_call.function.name
                if tool_call.function.arguments:
                    entry["function"]["arguments"] += tool_call.function.arguments

    if content:
        print()
    return content, [tool_calls[index] for index in sorted(tool_calls)]


def main() -> None:
    try:
        client, model = _load_client()
    except Exception as exc:
        print(f"启动失败：{exc}")
        return

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    print("本地文件管理助手已启动。")
    print(f"工作区：{WORKSPACE}")
    print("输入问题开始对话，输入 exit / quit 退出。")

    while True:
        try:
            question = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break

        if not question:
            continue
        if question.lower() in {"exit", "quit", "退出"}:
            print("再见。")
            break

        turn_start = len(messages)
        messages.append({"role": "user", "content": question})
        stopped = False

        for _ in range(MAX_TOOL_ROUNDS):
            try:
                content, tool_calls = stream_chat(client, model, messages)
            except Exception as exc:
                print(f"\n[错误] 调用模型失败：{exc}")
                del messages[turn_start:]
                stopped = True
                break

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": content or None,
            }
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            messages.append(assistant_message)

            if not tool_calls:
                break

            run_tool_calls(tool_calls, messages)
        else:
            messages.append(
                {
                    "role": "assistant",
                    "content": "工具调用次数达到上限，已停止本轮操作。",
                }
            )
            print("\n[提示] 工具调用次数达到上限，已停止本轮操作。")

        if stopped:
            continue


# ---------------------------------------------------------------------------
# 本地自检：不调用大模型，直接验证 10 个工具
# ---------------------------------------------------------------------------


def _self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="file-tools-", dir=WORKSPACE) as temp:
        root = Path(temp)
        checks: list[str] = []

        def check(label: str, condition: bool) -> None:
            if not condition:
                raise AssertionError(f"自检失败：{label}")
            checks.append(label)

        check("获取当前工作目录", str(WORKSPACE) in get_current_directory())

        data_dir = root / "data"
        nested = data_dir / "sub"
        check("创建目录", "已创建目录" in create_directory(str(nested)))
        check(
            "创建文件",
            "已创建文件" in create_file(str(data_dir), "note.txt", "hello\nworld\n"),
        )
        check("列出目录", "note.txt" in list_directory(str(data_dir), recursive=True))

        file_type = get_file_type(str(data_dir / "note.txt"))
        check("获取文件类型", "纯文本" in file_type and "utf-8" in file_type)

        content = read_text_file(str(data_dir / "note.txt"))
        check("读取文件", "hello" in content and "world" in content)

        check(
            "追加内容",
            "已追加内容" in edit_text_file(str(data_dir / "note.txt"), "append", content="tail\n"),
        )
        check(
            "替换内容",
            "已替换 1 处" in edit_text_file(
                str(data_dir / "note.txt"),
                "replace",
                old_text="hello",
                new_text="你好",
            ),
        )
        check("读取编辑结果", "你好" in read_text_file(str(data_dir / "note.txt")))

        check(
            "重命名文件",
            "已重命名文件" in rename_path(str(data_dir / "note.txt"), "note2.txt"),
        )
        check(
            "重命名目录",
            "已重命名目录" in rename_path(str(nested), "sub2"),
        )

        extra = data_dir / "delete-me.txt"
        create_file(str(data_dir), extra.name, "temporary")
        check("删除文件", "已删除文件" in delete_file(str(extra)))
        check("删除非空目录", "已删除目录" in delete_directory(str(data_dir)))

    for index, label in enumerate(checks, start=1):
        print(f"{index:02d}. {label} ... 通过")
    print(f"\n自检全部通过，共 {len(checks)} 项。")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        try:
            raise SystemExit(_self_test())
        except Exception as exc:
            print(f"自检失败：{exc}")
            raise SystemExit(1)
    main()
