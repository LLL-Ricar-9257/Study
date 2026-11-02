"""``loadSkill`` / ``createSkill`` 工具：加载或创建 Agent Skill。

``loadSkill`` 的安全边界：所有读取最终都必须解析到指定 Skill 的目录内部；
绝对路径、项目相对路径和 ``..`` 片段都不会绕过这个限制。

``createSkill`` 只负责 ``.agents/skills/<name>/SKILL.md`` 这一份最小骨架，
不创建 references/、scripts/ 等附加目录；模型需要时继续调用文件工具。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from ..config import AgentLimits
from ..errors import ToolError
from ..skills import (
    SKILL_FILENAME,
    SKILLS_RELATIVE_PATH,
    SkillInfo,
    SkillsCatalog,
    normalize_skill_name,
    read_skill_metadata,
    render_skill_markdown,
    split_skill_frontmatter,
    split_skill_markdown,
    strip_frontmatter_fields,
)
from .filesystem import detect_encoding, human_size, looks_like_text


class SkillToolset:
    """绑定 SkillsCatalog 的 ``loadSkill`` / ``createSkill`` 实现。"""

    def __init__(
        self,
        catalog: SkillsCatalog,
        *,
        project_root: str | Path | None = None,
        workspace_root: str | Path | None = None,
        skills_root: str | Path | None = None,
        limits: AgentLimits | None = None,
    ) -> None:
        self.catalog = catalog
        base = project_root if project_root is not None else catalog.project_root
        self.project_root = Path(base).expanduser().resolve()
        self.workspace_root = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None
            else None
        )
        self.skills_root = (
            Path(skills_root).expanduser().resolve()
            if skills_root is not None
            else self.project_root / SKILLS_RELATIVE_PATH
        )
        self.limits = limits or AgentLimits()

    def load_skill(self, name: str, filename: str | None = None) -> str:
        """加载 SKILL.md 全文；提供 filename 时改为读取对应附件。"""
        skill = self._require_skill(name)
        attachment = (filename or "").strip()
        if not attachment:
            return self._present(skill, skill.skill_file, "SKILL.md")

        located = self._locate_attachment(skill, attachment)
        if isinstance(located, list):
            return self._ambiguous_attachment(skill, attachment, located)
        return self._present(skill, located, "附件")

    def create_skill(self, name: str, skillContext: str) -> str:
        """在当前项目的 ``.agents/skills/<name>/`` 下创建 SKILL.md。

        参数名必须与 Function Calling 声明一致（``skillContext``），否则
        ``ToolRegistry`` 的参数绑定会失败。已存在的 Skill 一律拒绝覆盖。
        """
        try:
            slug = normalize_skill_name(name)
        except ValueError as exc:
            raise ToolError(f"Skill 名称不合法：{exc}") from exc

        context = self._validate_skill_context(skillContext)
        try:
            fields, body = split_skill_markdown(context)
            raw_lines, _ = split_skill_frontmatter(context)
        except ValueError as exc:
            raise ToolError(f"skillContext 格式不合法：{exc}") from exc

        # Frontmatter 键按 YAML 规则区分大小写，但 Agent Skills 约定使用小写；
        # 这里用大小写不敏感的方式读取 name / description，避免生成重复字段。
        normalized_fields = {key.casefold(): value for key, value in fields.items()}
        declared = str(normalized_fields.get("name", "")).strip()
        if declared:
            try:
                declared_slug = normalize_skill_name(declared)
            except ValueError as exc:
                raise ToolError(
                    f"skillContext 的 Frontmatter name 不合法：{exc}"
                ) from exc
            if declared_slug != slug:
                raise ToolError(
                    f"skillContext 的 Frontmatter name 是 '{declared}'，"
                    f"与参数 name 规范化后的 '{slug}' 不一致。"
                    "请统一两者后重试，工具不会擅自改写 Skill 名称。"
                )

        description = str(normalized_fields.get("description", "")).strip()
        if not description:
            description = _derive_description(body)
        if not description:
            raise ToolError(
                "skillContext 缺少 description：请在 YAML Frontmatter 中提供 "
                "description，或在正文首段写明这个 Skill 的用途。"
            )

        extra_lines = (
            strip_frontmatter_fields(raw_lines, {"name", "description"})
            if raw_lines is not None
            else ()
        )
        try:
            markdown = render_skill_markdown(
                slug,
                description,
                body,
                extra_lines=extra_lines,
            )
        except ValueError as exc:
            raise ToolError(f"无法生成合法的 SKILL.md：{exc}") from exc

        existing = self._find_existing_skill(slug)
        if existing is not None:
            raise ToolError(self._exists_message(slug, existing))

        directory = self.skills_root / slug
        target = directory / SKILL_FILENAME
        try:
            self.skills_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError(
                f"创建 Skill 根目录失败：{self.skills_root}：{exc}"
            ) from exc
        try:
            directory.mkdir()
        except FileExistsError as exc:
            raise ToolError(self._exists_message(slug, directory)) from exc
        except OSError as exc:
            raise ToolError(
                f"创建 Skill 目录失败：{directory}：{exc}"
            ) from exc

        try:
            # 独占创建：即使与并发调用竞争，也绝不覆盖已有 SKILL.md。
            with target.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(markdown)
        except FileExistsError as exc:
            raise ToolError(self._exists_message(slug, directory)) from exc
        except OSError as exc:
            _remove_empty_directory(directory)
            raise ToolError(f"写入 SKILL.md 失败：{target}：{exc}") from exc

        metadata = read_skill_metadata(target)
        if metadata is None or metadata[0] != slug or not metadata[1]:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            _remove_empty_directory(directory)
            raise ToolError(
                f"生成的 SKILL.md 未通过格式校验，已撤销创建：{target}"
            )

        size = len(markdown.encode("utf-8"))
        lines = markdown.count("\n")
        return (
            f"已创建 Skill：{slug}\n"
            f"目录：{directory}\n"
            f"文件：{target}\n"
            f"Frontmatter name：{metadata[0]}\n"
            f"Frontmatter description：{metadata[1]}\n"
            f"大小：{human_size(size)}；行数：{lines}\n"
            "说明：本轮系统提示中的 Skill 清单不会立即刷新，"
            "新 Skill 会在下次启动或新会话的可用 Skill 列表中生效。\n"
            "如需添加 references/、scripts/ 等目录或辅助文件，请继续调用 "
            "create_directory、create_file 等文件工具，目标目录为："
            f"{directory}"
        )

    def registry_handlers(self) -> dict[str, Callable[..., str]]:
        return {
            "loadSkill": self.load_skill,
            "createSkill": self.create_skill,
        }

    # -- 创建辅助 ---------------------------------------------------------

    def _validate_skill_context(self, value: str) -> str:
        if not isinstance(value, str):
            raise ToolError("skillContext 必须是字符串")
        text = value.strip()
        if not text:
            raise ToolError(
                "skillContext 不能为空，应传入 SKILL.md 的完整内容"
                "（YAML Frontmatter + 正文）"
            )
        if "\x00" in text:
            raise ToolError("skillContext 包含空字符")
        size = len(text.encode("utf-8"))
        if size > self.limits.max_text_bytes:
            raise ToolError(
                f"skillContext 过大（{human_size(size)}），"
                f"超过 {human_size(self.limits.max_text_bytes)} 的上限"
            )
        return text

    def _find_existing_skill(self, slug: str) -> Path | None:
        """返回已占用的 Skill 路径：已注册 Skill、同名目录都算冲突。"""
        for skill in self.catalog.skills:
            if (
                skill.name == slug
                or skill.directory.name.casefold() == slug.casefold()
            ):
                return skill.directory

        if not self.skills_root.is_dir():
            return None
        try:
            entries = list(self.skills_root.iterdir())
        except OSError as exc:
            raise ToolError(f"无法读取 Skill 目录：{self.skills_root}：{exc}") from exc
        for entry in entries:
            if entry.name.casefold() == slug.casefold():
                return entry
        return None

    def _exists_message(self, slug: str, path: Path) -> str:
        return (
            f"Skill '{slug}' 已存在，未创建任何文件，避免覆盖原有内容。\n"
            f"位置：{path}\n"
            "如需修改现有 Skill，请使用 read_text_file / edit_text_file 等文件工具；"
            "如需新建其他 Skill，请换一个 name。"
        )

    # -- 查找 -------------------------------------------------------------

    def _require_skill(self, name: str) -> SkillInfo:
        text = (name or "").strip()
        if not text:
            raise ToolError("name 不能为空，必须是系统提示中列出的 Skill 名称")
        skill = self.catalog.get(text)
        if skill is not None:
            return skill
        available = "、".join(skill.name for skill in self.catalog.skills) or "（无）"
        raise ToolError(f"未找到 Skill：{text}。当前可用 Skill：{available}")

    def _locate_attachment(self, skill: SkillInfo, filename: str) -> Path | list[Path]:
        if "\x00" in filename:
            raise ToolError("filename 包含空字符")
        try:
            raw = Path(os.path.expanduser(filename))
        except (OSError, RuntimeError) as exc:
            raise ToolError(f"无法解析 filename：{filename}：{exc}") from exc

        outside_attempt = False
        for candidate in self._exact_candidates(skill, raw):
            resolved = self._safe_resolve(candidate)
            if resolved is None:
                continue
            if not self._is_within(resolved, skill.directory):
                # 只有确实存在、但落在 Skill 之外的文件才算越界；
                # 项目相对路径本身通常不指向 Skill 目录，不应误报。
                if resolved.exists():
                    outside_attempt = True
                continue
            if resolved.is_file():
                return resolved

        if self._is_plain_name(filename):
            matches = self._search_by_name(skill, filename)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                return matches

        if outside_attempt:
            raise ToolError(
                f"filename 解析到了 Skill 目录之外：{filename}；"
                f"只能读取 {skill.directory} 内部的文件"
            )
        raise ToolError(
            f"在 Skill '{skill.name}' 的目录中找不到附件：{filename}"
        )

    def _exact_candidates(self, skill: SkillInfo, raw: Path) -> tuple[Path, ...]:
        if raw.is_absolute():
            return (raw,)

        candidates: list[Path] = [skill.directory / raw]
        for root in (skill.root, self.project_root, self.workspace_root):
            if root is None:
                continue
            candidate = root / raw
            if candidate not in candidates:
                candidates.append(candidate)
        return tuple(candidates)

    def _search_by_name(self, skill: SkillInfo, filename: str) -> list[Path]:
        target = filename.casefold()
        exact: list[Path] = []
        folded: list[Path] = []
        try:
            entries = skill.directory.rglob("*")
            for entry in entries:
                if not entry.is_file():
                    continue
                if entry.name == filename:
                    exact.append(entry)
                elif entry.name.casefold() == target:
                    folded.append(entry)
        except OSError as exc:
            raise ToolError(f"搜索 Skill 附件失败：{exc}") from exc

        matches: list[Path] = []
        for entry in exact or folded:
            resolved = self._safe_resolve(entry)
            if resolved is not None and self._is_within(resolved, skill.directory):
                matches.append(resolved)

        return sorted(
            set(matches),
            key=lambda path: path.relative_to(skill.directory).as_posix().casefold(),
        )

    # -- 读取与展示 -------------------------------------------------------

    def _present(self, skill: SkillInfo, target: Path, kind: str) -> str:
        text, encoding = self._read_text(target)
        try:
            relative = target.relative_to(skill.directory).as_posix()
        except ValueError:
            relative = target.name
        return (
            f"Skill：{skill.name}\n"
            f"类型：{kind}\n"
            f"文件：{target}\n"
            f"相对路径：{relative}\n"
            f"编码：{encoding}\n"
            "--- 内容开始 ---\n"
            f"{text}\n"
            "--- 内容结束 ---"
        )

    def _read_text(self, target: Path) -> tuple[str, str]:
        if not target.is_file():
            raise ToolError(f"附件不是文件或不存在：{target}")
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
            raise ToolError(f"不是纯文本文件：{target}")
        encoding = detect_encoding(data)
        if encoding is None:
            raise ToolError(f"不是可识别的纯文本文件：{target}")
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError as exc:
            raise ToolError(f"解码文件失败：{target}：{exc}") from exc

    @staticmethod
    def _safe_resolve(path: Path) -> Path | None:
        try:
            return path.resolve(strict=False)
        except (OSError, RuntimeError):
            return None

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _is_plain_name(filename: str) -> bool:
        return (
            Path(filename).name == filename
            and "/" not in filename
            and "\\" not in filename
        )

    @staticmethod
    def _ambiguous_attachment(
        skill: SkillInfo, filename: str, matches: list[Path]
    ) -> str:
        lines = [
            f"Skill '{skill.name}' 中存在多个名为 '{filename}' 的文件，"
            "无法确定要读取哪一个。",
            "请使用 filename 参数指定以下候选路径之一：",
        ]
        for path in matches:
            lines.append(f"- {path.relative_to(skill.directory).as_posix()}")
        return "\n".join(lines)


def _derive_description(body: str) -> str:
    """正文没有 Frontmatter description 时，用首个非空段落生成描述。"""
    for paragraph in body.split("\n\n"):
        text = " ".join(
            line.strip().lstrip("#").strip() for line in paragraph.strip().splitlines()
        )
        text = " ".join(text.split())
        if text:
            return text
    return ""


def _remove_empty_directory(directory: Path) -> None:
    """创建失败时尽量清理空目录，不掩盖原始错误。"""
    try:
        directory.rmdir()
    except OSError:
        return

