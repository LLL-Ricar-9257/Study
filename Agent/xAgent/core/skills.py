"""Agent Skills 的发现与元数据解析。

Skill 约定：项目根目录（程序启动时的用户工作目录）下的
``.agents/skills/<skill-name>/SKILL.md``，文件开头是 YAML Frontmatter，
必须声明 ``name`` 和 ``description``。

初始化阶段只解析 Frontmatter，不读取 SKILL.md 正文；完整内容由
``loadSkill`` 工具在模型明确需要时动态加载。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

_SKILL_FILENAME = "SKILL.md"
_MAX_FRONTMATTER_LINES = 512
_MAX_FRONTMATTER_CHARS = 128 * 1024
_KEY_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$")
_BLOCK_PATTERN = re.compile(r"^[|>][+-]?\d*$")


@dataclass(frozen=True, slots=True)
class SkillInfo:
    """初始化阶段即可安全加载的 Skill 元数据。"""

    name: str
    description: str
    directory: Path
    skill_file: Path
    root: Path | None = None


@dataclass(frozen=True, slots=True)
class SkillsCatalog:
    """项目内发现的 Skill 集合。"""

    project_root: Path
    skills_root: Path
    skills: tuple[SkillInfo, ...] = ()
    issues: tuple[str, ...] = ()

    @classmethod
    def discover(
        cls,
        project_root: str | Path,
        extra_roots: Iterable[str | Path] = (),
    ) -> SkillsCatalog:
        """扫描 Skill 目录。

        以 ``project_root``（程序启动时的当前工作目录）为主，按顺序扫描
        ``<root>/.agents/skills``；``extra_roots`` 用于兼容从其他目录启动、
        但通过 ``--workspace`` 指定工作区的场景。同名 Skill 以先扫描到的为准。
        """
        roots = _resolve_roots(project_root, extra_roots)
        root = roots[0]
        skills_root = root / ".agents" / "skills"

        issues: list[str] = []
        skills: list[SkillInfo] = []
        seen: set[str] = set()

        for candidate_root in roots:
            _scan_skills_root(candidate_root, skills, seen, issues)

        return cls(
            project_root=root,
            skills_root=skills_root,
            skills=tuple(skills),
            issues=tuple(issues),
        )

    def get(self, name: str) -> SkillInfo | None:
        """按名称查找 Skill。

        精确匹配 Frontmatter ``name`` 优先，其次允许使用 Skill 目录名作为别名；
        最后做唯一的大小写不敏感匹配。
        """
        text = (name or "").strip()
        if not text:
            return None

        for skill in self.skills:
            if skill.name == text:
                return skill

        aliases = [skill for skill in self.skills if skill.directory.name == text]
        if len(aliases) == 1:
            return aliases[0]

        folded = [
            skill
            for skill in self.skills
            if skill.name.casefold() == text.casefold()
            or skill.directory.name.casefold() == text.casefold()
        ]
        return folded[0] if len(folded) == 1 else None

    def __iter__(self) -> Iterator[SkillInfo]:
        return iter(self.skills)

    def __len__(self) -> int:
        return len(self.skills)


def _resolve_roots(
    project_root: str | Path,
    extra_roots: Iterable[str | Path],
) -> list[Path]:
    values: list[str | Path] = [project_root]
    if isinstance(extra_roots, (str, Path)):
        values.append(extra_roots)
    else:
        values.extend(extra_roots)

    roots: list[Path] = []
    for value in values:
        try:
            resolved = Path(value).expanduser().resolve()
        except (OSError, RuntimeError):
            resolved = Path(value).expanduser()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _scan_skills_root(
    root: Path,
    skills: list[SkillInfo],
    seen: set[str],
    issues: list[str],
) -> None:
    skills_root = root / ".agents" / "skills"
    if not skills_root.is_dir():
        return

    try:
        directories = sorted(
            (entry for entry in skills_root.iterdir() if entry.is_dir()),
            key=lambda entry: entry.name.casefold(),
        )
    except OSError as exc:
        issues.append(f"无法读取 Skill 目录 {skills_root}：{exc}")
        return

    for directory in directories:
        skill_file = _find_skill_file(directory)
        if skill_file is None:
            issues.append(f"跳过 {directory.name}：缺少 SKILL.md")
            continue

        try:
            metadata = _read_metadata(skill_file)
        except OSError as exc:
            issues.append(f"跳过 {directory.name}：读取 SKILL.md 失败：{exc}")
            continue

        if metadata is None:
            issues.append(
                f"跳过 {directory.name}：SKILL.md 缺少合法的 YAML Frontmatter"
            )
            continue

        name, description = metadata
        if not name:
            issues.append(f"跳过 {directory.name}：Frontmatter 缺少 name")
            continue
        if not description:
            issues.append(f"跳过 {directory.name}：Frontmatter 缺少 description")
            continue
        if name in seen:
            issues.append(f"跳过 {directory.name}：Skill 名称重复：{name}")
            continue

        try:
            resolved_directory = directory.resolve()
            resolved_file = skill_file.resolve()
        except (OSError, RuntimeError) as exc:
            issues.append(f"跳过 {directory.name}：无法解析 Skill 路径：{exc}")
            continue

        seen.add(name)
        skills.append(
            SkillInfo(
                name=name,
                description=description,
                directory=resolved_directory,
                skill_file=resolved_file,
                root=root,
            )
        )


def _find_skill_file(directory: Path) -> Path | None:
    exact = directory / _SKILL_FILENAME
    if exact.is_file():
        return exact
    try:
        candidates = sorted(directory.iterdir(), key=lambda entry: entry.name.casefold())
    except OSError:
        return None
    for entry in candidates:
        if entry.is_file() and entry.name.casefold() == _SKILL_FILENAME.casefold():
            return entry
    return None


def _read_metadata(path: Path) -> tuple[str, str] | None:
    lines = _read_frontmatter_lines(path)
    if not lines or len(lines) < 2:
        return None
    fields = _parse_mapping(lines[1:-1])
    name = str(fields.get("name", "")).strip()
    description = str(fields.get("description", "")).strip()
    return name, description


def _read_frontmatter_lines(path: Path) -> list[str] | None:
    """只读取到结束分隔符 ``---`` 为止，避免把正文加载进内存。"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            lines = _read_lines_until_fence(path, encoding)
        except UnicodeDecodeError:
            continue
        except (OSError, RuntimeError):
            return None
        return lines
    return None


def _read_lines_until_fence(path: Path, encoding: str) -> list[str] | None:
    lines: list[str] = []
    total = 0
    with path.open("r", encoding=encoding, newline=None) as handle:
        for index, line in enumerate(handle):
            if index >= _MAX_FRONTMATTER_LINES + 1:
                return None
            total += len(line)
            if total > _MAX_FRONTMATTER_CHARS:
                return None

            if index == 0:
                if line.lstrip("\ufeff").strip() != "---":
                    return None
                lines.append("---")
                continue

            raw = line.rstrip("\r\n")
            lines.append(raw)
            if raw.rstrip() == "---":
                return lines
    return None


def _parse_mapping(lines: list[str]) -> dict[str, str]:
    """解析 Frontmatter 中扁平的 key/value 映射，支持常用 YAML 标量写法。"""
    fields: dict[str, str] = {}
    index = 0
    while index < len(lines):
        raw = lines[index]
        if not raw.strip() or raw.lstrip().startswith("#"):
            index += 1
            continue
        match = _KEY_PATTERN.match(raw)
        if match is None:
            index += 1
            continue

        key = match.group(1)
        value = match.group(2).strip()
        base_indent = len(raw) - len(raw.lstrip())

        block: list[str] = []
        next_index = index + 1
        while next_index < len(lines):
            following = lines[next_index]
            if not following.strip():
                block.append("")
                next_index += 1
                continue
            indent = len(following) - len(following.lstrip())
            if indent <= base_indent:
                break
            block.append(following.strip())
            next_index += 1

        while block and not block[-1]:
            block.pop()

        fields[key] = _combine_scalar(value, block)
        index = next_index
    return fields


def _combine_scalar(value: str, block: list[str]) -> str:
    if _BLOCK_PATTERN.match(value):
        if value.startswith(">"):
            text = " ".join(part.strip() for part in block if part.strip())
        else:
            text = "\n".join(block)
        return text.strip()

    inline = _unquote(value)
    if block:
        continuation = " ".join(part.strip() for part in block if part.strip())
        inline = f"{inline} {continuation}".strip()
    return inline.strip()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value[1:-1].replace('\\"', '"').replace("\\n", "\n")
        return parsed if isinstance(parsed, str) else value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value
