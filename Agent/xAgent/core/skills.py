"""Agent Skills 的发现、元数据解析与 SKILL.md 生成。

Skill 约定：``<根目录>/.agents/skills/<skill-name>/SKILL.md``，文件开头是
YAML Frontmatter，必须声明 ``name`` 和 ``description``。

扫描根按优先级排列：启动目录 → ``--workspace`` 工作区 → xAgent 自带目录
（``<安装目录>/.agents/skills``）。自带目录让 Skill 随源码一起分发，任何
目录启动、任何工作区都能看到；同名 Skill 以先扫描到的为准。

初始化阶段只解析 Frontmatter，不读取 SKILL.md 正文；完整内容由
``loadSkill`` 工具在模型明确需要时动态加载。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

SKILL_FILENAME = "SKILL.md"
SKILLS_RELATIVE_PATH = Path(".agents") / "skills"
MAX_SKILL_NAME_LENGTH = 64
_MAX_FRONTMATTER_LINES = 512
_MAX_FRONTMATTER_CHARS = 128 * 1024
_KEY_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$")
_BLOCK_PATTERN = re.compile(r"^[|>][+-]?\d*$")
_FRONTMATTER_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_YAML_INDICATORS = tuple("-?:,[]{}#&*!|>'\"%@`")


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
    searched_roots: tuple[Path, ...] = ()

    @classmethod
    def discover(
        cls,
        project_root: str | Path,
        extra_roots: Iterable[str | Path] = (),
        bundled_roots: Iterable[str | Path] | str | Path = (),
    ) -> SkillsCatalog:
        """扫描 Skill 目录。

        按优先级依次扫描 ``<root>/.agents/skills``：``project_root``
        （程序启动时的当前工作目录）→ ``extra_roots``（通常是 ``--workspace``
        工作区）→ ``bundled_roots``（xAgent 自带目录）。同名 Skill 以先扫描到的
        为准，重复的根目录只扫描一次。
        """
        roots = _resolve_roots(project_root, extra_roots, bundled_roots)
        root = roots[0]
        skills_root = root / SKILLS_RELATIVE_PATH

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
            searched_roots=tuple(roots),
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


def bundled_root() -> Path:
    """xAgent 自带 Skill 的扫描根：``core`` 包的上一级目录（源码仓库根）。

    扫描根下的 ``.agents/skills`` 就是内置技能目录。这样 Skill 可以随代码一起
    分发，而不依赖程序的启动目录或 ``--workspace``。
    """
    return Path(__file__).resolve().parent.parent


def normalize_skill_name(name: str) -> str:
    """把用户给出的 Skill 名称规范成目录名：小写字母、数字和连字符。

    允许 ``PDF Tools``、``pdf_tools`` 这类写法，统一转换为 ``pdf-tools``。
    中文等无法转换为 ASCII 的名称会被拒绝，让模型改传英文 slug，避免生成
    不可移植的目录名。
    """
    if not isinstance(name, str):
        raise ValueError("name 必须是字符串")

    raw = unicodedata.normalize("NFKD", name)
    raw = "".join(char for char in raw if not unicodedata.combining(char)).strip()
    if not raw:
        raise ValueError("name 不能为空")
    if any(character in raw for character in ("/", "\\", "\x00")):
        raise ValueError("name 不能包含路径分隔符或空字符")

    text = re.sub(r"[\s_.]+", "-", raw.casefold())
    text = re.sub(r"[^a-z0-9-]+", "", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if len(text) > MAX_SKILL_NAME_LENGTH:
        text = text[:MAX_SKILL_NAME_LENGTH].rstrip("-")
    if not text:
        raise ValueError(
            "name 必须能转换为小写字母、数字和连字符组成的英文名称，"
            '例如 "write-weekly-report"；中文名称请同时给出对应的英文 slug'
        )
    return text


def split_skill_frontmatter(text: str) -> tuple[tuple[str, ...] | None, str]:
    """把 SKILL.md 文本拆成原始 Frontmatter 行与正文。

    返回的 ``None`` 表示文本没有 Frontmatter（整段都是正文）；以 ``---``
    开头却找不到结束分隔符时抛出 ``ValueError``，避免把坏掉的 Frontmatter
    写进正文。
    """
    if not isinstance(text, str):
        raise ValueError("skillContext 必须是字符串")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, normalized

    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return tuple(lines[1:index]), "\n".join(lines[index + 1 :])
    raise ValueError("YAML Frontmatter 缺少结束分隔符 ---")


def split_skill_markdown(text: str) -> tuple[dict[str, str], str]:
    """拆出 Frontmatter 字段映射与正文；没有 Frontmatter 时字段映射为空。"""
    raw_lines, body = split_skill_frontmatter(text)
    if raw_lines is None:
        return {}, body
    return _parse_mapping(list(raw_lines)), body


def strip_frontmatter_fields(
    raw_lines: Sequence[str],
    keys: Iterable[str],
) -> tuple[str, ...]:
    """从原始 Frontmatter 行中移除指定字段（含其续行），其余内容原样保留。

    这样 ``license``、``allowed-tools`` 或嵌套的 ``metadata`` 等字段不会被
    重新序列化而丢失结构。
    """
    drop = {key.casefold() for key in keys}
    dropped_ranges = [
        (start, end)
        for key, _value, _block, start, end in _mapping_entries(list(raw_lines))
        if key.casefold() in drop
    ]
    kept: list[str] = []
    index = 0
    while index < len(raw_lines):
        matched = next(
            (end for start, end in dropped_ranges if start == index),
            None,
        )
        if matched is not None:
            index = matched
            continue
        kept.append(raw_lines[index])
        index += 1
    return tuple(kept)


def render_skill_markdown(
    name: str,
    description: str,
    body: str = "",
    extra_lines: Sequence[str] = (),
) -> str:
    """生成符合 Agent Skills 规范的 SKILL.md 文本。

    ``name`` 由调用方保证已经规范化；``description`` 会按 YAML 标量规则
    重新序列化，因此描述中的冒号、引号和换行不会破坏 Frontmatter 结构。
    ``extra_lines`` 是原样保留的其他 Frontmatter 行。
    """
    if not _FRONTMATTER_KEY_PATTERN.match(name):
        raise ValueError(f"Skill 名称不合法：{name}")

    summary = " ".join(str(description).split())
    if not summary:
        raise ValueError("SKILL.md 缺少 description")

    lines = ["---", f"name: {name}", f"description: {_yaml_scalar(summary)}"]
    for raw in extra_lines:
        if raw.strip() == "---":
            raise ValueError("Frontmatter 中不能包含分隔符 ---")
        if "\x00" in raw:
            raise ValueError("Frontmatter 包含空字符")
        lines.append(raw)
    lines.append("---")

    body_text = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not body_text.strip():
        raise ValueError("SKILL.md 缺少正文内容")
    return "\n".join(lines) + "\n\n" + body_text + "\n"


def read_skill_metadata(path: str | Path) -> tuple[str, str] | None:
    """读取 SKILL.md 的 name 与 description；文件格式不合法时返回 None。"""
    return _read_metadata(Path(path))


def _yaml_scalar(value: str) -> str:
    """尽量输出可读的普通标量；有歧义时退化为双引号字符串。"""
    text = " ".join(str(value).split())
    if (
        text
        and not text.startswith(_YAML_INDICATORS)
        and ": " not in text
        and " #" not in text
        and not text.endswith(":")
    ):
        return text
    return json.dumps(text, ensure_ascii=False)


def _resolve_roots(
    project_root: str | Path,
    extra_roots: Iterable[str | Path] = (),
    bundled_roots: Iterable[str | Path] | str | Path = (),
) -> list[Path]:
    values: list[str | Path] = [project_root]
    for group in (extra_roots, bundled_roots):
        if isinstance(group, (str, Path)):
            values.append(group)
        else:
            values.extend(group)

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
    skills_root = root / SKILLS_RELATIVE_PATH
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
    exact = directory / SKILL_FILENAME
    if exact.is_file():
        return exact
    try:
        candidates = sorted(directory.iterdir(), key=lambda entry: entry.name.casefold())
    except OSError:
        return None
    for entry in candidates:
        if entry.is_file() and entry.name.casefold() == SKILL_FILENAME.casefold():
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
    return {
        key: _combine_scalar(value, block)
        for key, value, block, _start, _end in _mapping_entries(lines)
    }


def _mapping_entries(
    lines: list[str],
) -> Iterator[tuple[str, str, list[str], int, int]]:
    """逐个产出 (key, 原始值, 续行, 起始行号, 结束行号)。"""
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

        yield key, value, block, index, next_index
        index = next_index


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
