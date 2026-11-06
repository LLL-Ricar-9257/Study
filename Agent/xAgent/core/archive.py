"""上下文归档与本地全文检索。

压缩上下文之前，先把当前会话的原始消息完整写入项目本地的
``.agents/context/<归档编号>.jsonl``：一行一条消息，保留角色、正文、工具调用
参数、工具返回结果和先后顺序。之后由一个很小的关键词检索替代向量数据库：
逐行读取 JSONL，对正文和工具信息做大小写无关的子串匹配，返回片段、归档编号
和消息位置。实现刻意保持简单，方便阅读和修改。

归档编号使用 ``YYYYMMDD-HHMMSS`` 形式（同一秒内重复时追加 ``-2``、``-3``），
既便于排序，也能直接作为文件名。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .context import estimate_messages_tokens
from .models import ChatMessage

CONTEXT_ARCHIVE_DIR = Path(".agents") / "context"
"""归档目录，相对于项目根目录。"""

ARCHIVE_ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}(?:-[0-9]+)?$")

MAX_RECALL_HITS = 6
"""单次 recallContext 最多返回的片段数。"""

MAX_RECALL_CHARS = 4_000
"""单次 recallContext 返回片段的总字符上限，避免检索结果再次占满上下文。"""

SNIPPET_PADDING = 120
MAX_SNIPPET_CHARS = 320


class ArchiveNotFoundError(ValueError):
    """指定的归档不存在，或目录里还没有任何归档。"""


@dataclass(frozen=True, slots=True)
class ArchivedContext:
    """一次归档的元数据。"""

    archive_id: str
    path: Path
    created_at: str
    """人类可读的本地时间，例如 ``2026-10-22 15:30:00``。"""

    message_count: int
    tokens: int
    """按本地规则估算的 token 数，仅用于展示规模。"""


@dataclass(frozen=True, slots=True)
class RecallHit:
    """一条命中记录。"""

    archive_id: str
    seq: int
    """消息在归档中的位置（从 1 开始，同时也是 JSONL 的行号）。"""

    role: str
    snippet: str
    score: int


@dataclass(frozen=True, slots=True)
class RecallResult:
    """一次检索的完整结果，供工具层格式化。"""

    query: str
    hits: tuple[RecallHit, ...]
    scanned_archives: int
    total_matches: int
    truncated: bool


def _unique_archive_id(directory: Path, when: datetime) -> str:
    base = when.strftime("%Y%m%d-%H%M%S")
    candidate = base
    suffix = 2
    while (directory / f"{candidate}.jsonl").exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _record(message: ChatMessage, index: int) -> dict[str, object]:
    return {
        "seq": index + 1,
        "role": message.role,
        "content": message.content,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        ],
        "tool_call_id": message.tool_call_id,
    }


def _write_atomic(path: Path, lines: list[str]) -> None:
    """先写临时文件再原子替换，避免中途失败留下半个归档。"""

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


def _searchable_text(record: dict[str, object]) -> str:
    parts: list[str] = []
    content = record.get("content")
    if isinstance(content, str) and content:
        parts.append(content)
    calls = record.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = call.get("name")
            arguments = call.get("arguments")
            if isinstance(name, str):
                parts.append(name)
            if isinstance(arguments, str):
                parts.append(arguments)
    return "\n".join(parts)


def _iter_records(path: Path):
    """逐行读取 JSONL；损坏的行直接跳过，避免一个坏文件阻塞全部检索。"""

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(record, dict):
            yield record


def _snippet(text: str, terms: list[str]) -> str:
    lowered = text.casefold()
    positions = [
        lowered.find(term.casefold())
        for term in terms
        if lowered.find(term.casefold()) >= 0
    ]
    if not positions:
        return ""
    start = max(min(positions) - SNIPPET_PADDING, 0)
    end = min(start + MAX_SNIPPET_CHARS, len(text))
    if end - start < MAX_SNIPPET_CHARS:
        start = max(end - MAX_SNIPPET_CHARS, 0)
    snippet = " ".join(text[start:end].split())
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


class ContextArchive:
    """项目本地的上下文归档目录。

    ``root`` 是项目根目录（与 ``.agents/skills`` 使用同一个根），归档实际写入
    ``root/.agents/context/``。所有方法都是同步的，工具层会在线程里调用。
    """

    def __init__(self, root: str | Path, directory: str | Path = CONTEXT_ARCHIVE_DIR) -> None:
        self._root = Path(root).expanduser().resolve()
        candidate = Path(directory).expanduser()
        self._directory = candidate if candidate.is_absolute() else self._root / candidate

    @property
    def root(self) -> Path:
        return self._root

    @property
    def directory(self) -> Path:
        return self._directory

    def archive_files(self) -> tuple[Path, ...]:
        """按编号升序返回全部归档文件（最新的一份在最后）。"""

        directory = self.directory
        if not directory.is_dir():
            return ()
        return tuple(
            sorted(
                (
                    path
                    for path in directory.glob("*.jsonl")
                    if ARCHIVE_ID_PATTERN.match(path.stem)
                ),
                key=lambda path: path.stem,
            )
        )

    def archive_ids(self) -> tuple[str, ...]:
        return tuple(path.stem for path in self.archive_files())

    def save(
        self,
        messages: list[ChatMessage] | tuple[ChatMessage, ...],
        *,
        when: datetime | None = None,
        archive_id: str | None = None,
    ) -> ArchivedContext:
        """把整段消息按顺序写成 JSONL，返回归档元数据。

        写入失败会抛异常，调用方据此中止压缩；``os.replace`` 保证只有完整写入
        后归档才会出现在目录里。
        """

        messages = tuple(messages)
        if not messages:
            raise ValueError("没有可归档的上下文")
        created = when or datetime.now()
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)

        if archive_id:
            archive_id = archive_id.strip()
            if not ARCHIVE_ID_PATTERN.match(archive_id):
                raise ValueError(
                    "归档编号不合法："
                    f"{archive_id}（示例：20261022-153000）"
                )
            path = directory / f"{archive_id}.jsonl"
            if path.exists():
                raise ValueError(f"归档已存在：{archive_id}")
        else:
            archive_id = _unique_archive_id(directory, created)
            path = directory / f"{archive_id}.jsonl"

        lines = [
            json.dumps(_record(message, index), ensure_ascii=False)
            for index, message in enumerate(messages)
        ]
        _write_atomic(path, lines)
        return ArchivedContext(
            archive_id=archive_id,
            path=path,
            created_at=created.strftime("%Y-%m-%d %H:%M:%S"),
            message_count=len(messages),
            tokens=estimate_messages_tokens(messages),
        )

    def recall(
        self,
        text: str,
        archive_id: str | None = None,
        *,
        limit: int = MAX_RECALL_HITS,
        max_chars: int = MAX_RECALL_CHARS,
    ) -> RecallResult:
        """在指定归档或全部归档中做关键词检索。

        ``text`` 按空白拆分为多个关键词：命中关键词越多、出现次数越多的消息
        排名越靠前；返回片段数量和总长度都有上限。
        """

        query = (text or "").strip()
        if not query:
            raise ValueError("检索关键词 text 不能为空")
        terms = [term for term in query.split() if term] or [query]
        if query not in terms:
            terms.append(query)

        if archive_id:
            identifier = archive_id.strip()
            if not ARCHIVE_ID_PATTERN.match(identifier):
                raise ValueError(
                    "归档编号不合法："
                    f"{identifier}（示例：20261022-153000）"
                )
            path = self.directory / f"{identifier}.jsonl"
            if not path.is_file():
                available = "、".join(self.archive_ids()[-5:]) or "无"
                raise ArchiveNotFoundError(
                    f"找不到归档 {identifier}；现有归档：{available}"
                )
            files: list[tuple[str, Path]] = [(identifier, path)]
        else:
            files = [(path.stem, path) for path in reversed(self.archive_files())]
            if not files:
                raise ArchiveNotFoundError(
                    "还没有任何上下文归档；执行 /compact 压缩一次后才会产生归档。"
                )

        matches: list[RecallHit] = []
        for identifier, path in files:
            for record in _iter_records(path):
                haystack = _searchable_text(record)
                if not haystack:
                    continue
                lowered = haystack.casefold()
                matched = [term for term in terms if term.casefold() in lowered]
                if not matched:
                    continue
                occurrences = sum(
                    lowered.count(term.casefold()) for term in matched
                )
                matches.append(
                    RecallHit(
                        archive_id=identifier,
                        seq=int(record.get("seq") or 0),
                        role=str(record.get("role") or ""),
                        snippet=_snippet(haystack, matched),
                        score=len(matched) * 1_000 + occurrences,
                    )
                )

        # 先按归档/位置倒序（新的优先），再按分数稳定排序，分数相同时保留新记录。
        matches.sort(key=lambda hit: (hit.archive_id, hit.seq), reverse=True)
        matches.sort(key=lambda hit: hit.score, reverse=True)

        selected: list[RecallHit] = []
        used = 0
        for hit in matches:
            if len(selected) >= limit:
                break
            if selected and used + len(hit.snippet) > max_chars:
                break
            selected.append(hit)
            used += len(hit.snippet)

        return RecallResult(
            query=query,
            hits=tuple(selected),
            scanned_archives=len(files),
            total_matches=len(matches),
            truncated=len(matches) > len(selected),
        )


__all__ = [
    "ARCHIVE_ID_PATTERN",
    "CONTEXT_ARCHIVE_DIR",
    "MAX_RECALL_CHARS",
    "MAX_RECALL_HITS",
    "ArchiveNotFoundError",
    "ArchivedContext",
    "ContextArchive",
    "RecallHit",
    "RecallResult",
]
