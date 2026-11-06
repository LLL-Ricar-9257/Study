"""会话事件日志与项目指令文件（AGENTS.md）。

每个会话对应 ``.agents/sessions/<会话编号>.jsonl``，第一行是会话头，之后
每条消息一行事件。日志使用「临时文件 + ``os.replace``」整体重写：实现简单，
也能保证进程中断时不会留下半个会话。

``AGENTS.md`` 在会话首次加载时被读取一次，作为一条消息写入历史并随会话日志
持久化；恢复会话或后续轮次直接复用日志里的内容，不会重复注入，避免破坏
Prompt 缓存。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import ChatMessage, ToolCall

SESSION_ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}(?:-[0-9]+)?$")

AGENTS_MD_MESSAGE_PREFIX = (
    "以下是从项目根目录读取的 AGENTS.md 项目指令，"
    "请在本次会话中始终遵守；后续轮次无需重复提供。"
)

AGENTS_MD_TRUNCATED_NOTE = "\n\n[AGENTS.md 内容过长，已截断。]"


class SessionNotFoundError(ValueError):
    """指定的会话不存在或编号不合法。"""


@dataclass(frozen=True, slots=True)
class AgentsDocument:
    """启动时读取到的项目指令文件。"""

    path: Path
    content: str
    truncated: bool = False

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """会话列表中的一项。"""

    session_id: str
    path: Path
    created_at: str
    updated_at: str
    title: str
    message_count: int


@dataclass(frozen=True, slots=True)
class LoadedSession:
    """从事件日志恢复出来的会话。"""

    session_id: str
    path: Path
    created_at: str
    updated_at: str
    title: str
    messages: tuple[ChatMessage, ...]
    agents_md_path: str = ""


def agents_md_message(content: str) -> ChatMessage:
    """构造注入 AGENTS.md 的那条消息。"""

    return ChatMessage.user(f"{AGENTS_MD_MESSAGE_PREFIX}\n\n{content.strip()}")


def is_agents_md_message(message: ChatMessage) -> bool:
    return message.role == "user" and (message.content or "").startswith(
        AGENTS_MD_MESSAGE_PREFIX
    )


def load_agents_md(
    root: str | Path,
    *,
    filename: str = "AGENTS.md",
    max_chars: int = 20_000,
) -> AgentsDocument | None:
    """读取项目根目录下的 AGENTS.md；不存在或为空时返回 ``None``。"""

    path = Path(root).expanduser().resolve() / filename
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = text.strip()
    if not text:
        return None
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars] + AGENTS_MD_TRUNCATED_NOTE
        truncated = True
    return AgentsDocument(path=path, content=text, truncated=truncated)


def _record(message: ChatMessage) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        ],
        "tool_call_id": message.tool_call_id,
    }


def _message_from_record(record: object) -> ChatMessage | None:
    if not isinstance(record, dict):
        return None
    role = record.get("role")
    if role not in {"system", "user", "assistant", "tool"}:
        return None
    content = record.get("content")
    if content is not None and not isinstance(content, str):
        return None
    calls: list[ToolCall] = []
    raw_calls = record.get("tool_calls")
    if isinstance(raw_calls, list):
        for raw in raw_calls:
            if not isinstance(raw, dict):
                continue
            name = raw.get("name")
            if not isinstance(name, str):
                continue
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or ""),
                    name=name,
                    arguments=str(raw.get("arguments") or ""),
                )
            )
    tool_call_id = record.get("tool_call_id")
    return ChatMessage(
        role=role,  # type: ignore[arg-type]
        content=content,
        tool_calls=tuple(calls),
        tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
    )


def _derive_title(messages: tuple[ChatMessage, ...]) -> str:
    for message in messages:
        if message.role != "user" or is_agents_md_message(message):
            continue
        text = " ".join((message.content or "").split())
        if text:
            return text[:60]
    return "新会话"


def _write_atomic(path: Path, lines: list[str]) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


class SessionStore:
    """项目本地的会话目录。

    ``directory`` 可以是相对项目根目录的路径（默认 ``.agents/sessions``），
    也可以是绝对路径。
    """

    def __init__(self, root: str | Path, directory: str | Path = ".agents/sessions") -> None:
        self._root = Path(root).expanduser().resolve()
        candidate = Path(directory).expanduser()
        self._directory = (
            candidate if candidate.is_absolute() else self._root / candidate
        )

    @property
    def root(self) -> Path:
        return self._root

    @property
    def directory(self) -> Path:
        return self._directory

    def new_id(self, *, when: datetime | None = None) -> str:
        base = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
        candidate = base
        suffix = 2
        while (self.directory / f"{candidate}.jsonl").exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def save(
        self,
        session_id: str,
        messages: list[ChatMessage] | tuple[ChatMessage, ...],
        *,
        created_at: str = "",
        title: str = "",
        workspace: str = "",
        agents_md_path: str = "",
        when: datetime | None = None,
    ) -> SessionSummary:
        """把整段会话写成事件日志并返回摘要。"""

        if not SESSION_ID_PATTERN.match(session_id):
            raise ValueError(
                f"会话编号不合法：{session_id}（示例：20261022-153000）"
            )
        messages = tuple(messages)
        if not messages:
            raise ValueError("没有可保存的会话内容")
        now = when or datetime.now()
        updated_at = now.strftime("%Y-%m-%d %H:%M:%S")
        created = created_at or updated_at
        effective_title = title.strip() or _derive_title(messages)

        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{session_id}.jsonl"
        header = {
            "type": "session",
            "version": 1,
            "id": session_id,
            "created_at": created,
            "updated_at": updated_at,
            "title": effective_title,
            "workspace": workspace,
            "agents_md": agents_md_path,
        }
        lines = [json.dumps(header, ensure_ascii=False)]
        if agents_md_path:
            lines.append(
                json.dumps(
                    {"type": "agents_md", "at": updated_at, "path": agents_md_path},
                    ensure_ascii=False,
                )
            )
        for index, message in enumerate(messages, start=1):
            lines.append(
                json.dumps(
                    {
                        "type": "message",
                        "at": updated_at,
                        "seq": index,
                        "message": _record(message),
                    },
                    ensure_ascii=False,
                )
            )
        _write_atomic(path, lines)
        return SessionSummary(
            session_id=session_id,
            path=path,
            created_at=created,
            updated_at=updated_at,
            title=effective_title,
            message_count=len(messages),
        )

    def list_sessions(self, *, limit: int = 50) -> tuple[SessionSummary, ...]:
        """按更新时间倒序返回会话列表；损坏的文件会被跳过。"""

        directory = self.directory
        if not directory.is_dir():
            return ()
        summaries: list[SessionSummary] = []
        for path in directory.glob("*.jsonl"):
            if not SESSION_ID_PATTERN.match(path.stem):
                continue
            summary = self._read_summary(path)
            if summary is not None:
                summaries.append(summary)
        summaries.sort(key=lambda item: (item.updated_at, item.session_id), reverse=True)
        return tuple(summaries[:limit])

    def load(self, session_id: str) -> LoadedSession:
        identifier = (session_id or "").strip()
        if not SESSION_ID_PATTERN.match(identifier):
            raise SessionNotFoundError(f"会话编号不合法：{session_id}")
        path = self.directory / f"{identifier}.jsonl"
        if not path.is_file():
            raise SessionNotFoundError(f"找不到会话：{identifier}")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SessionNotFoundError(f"读取会话失败：{identifier}：{exc}") from exc

        header: dict[str, object] = {}
        messages: list[ChatMessage] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") == "session" and not header:
                header = record
            elif record.get("type") == "message":
                message = _message_from_record(record.get("message"))
                if message is not None:
                    messages.append(message)
        if not header or not messages:
            raise SessionNotFoundError(f"会话日志为空或已损坏：{identifier}")
        return LoadedSession(
            session_id=identifier,
            path=path,
            created_at=str(header.get("created_at") or ""),
            updated_at=str(header.get("updated_at") or ""),
            title=str(header.get("title") or _derive_title(tuple(messages))),
            messages=tuple(messages),
            agents_md_path=str(header.get("agents_md") or ""),
        )

    @staticmethod
    def _read_summary(path: Path) -> SessionSummary | None:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                first = handle.readline().strip()
                if not first:
                    return None
                header = json.loads(first)
                if not isinstance(header, dict) or header.get("type") != "session":
                    return None
                count = 0
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(record, dict) and record.get("type") == "message":
                        count += 1
        except (OSError, ValueError):
            return None
        created = str(header.get("created_at") or "")
        updated = str(header.get("updated_at") or created)
        return SessionSummary(
            session_id=path.stem,
            path=path,
            created_at=created,
            updated_at=updated,
            title=str(header.get("title") or "新会话"),
            message_count=count,
        )


__all__ = [
    "AGENTS_MD_MESSAGE_PREFIX",
    "SESSION_ID_PATTERN",
    "AgentsDocument",
    "LoadedSession",
    "SessionNotFoundError",
    "SessionStore",
    "SessionSummary",
    "agents_md_message",
    "is_agents_md_message",
    "load_agents_md",
]
