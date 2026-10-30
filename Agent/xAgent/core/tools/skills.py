"""``loadSkill`` 工具：按需加载 Skill 正文或 Skill 目录内的附件。

安全边界：所有读取最终都必须解析到指定 Skill 的目录内部；绝对路径、
项目相对路径和 ``..`` 片段都不会绕过这个限制。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from ..config import AgentLimits
from ..errors import ToolError
from ..skills import SkillInfo, SkillsCatalog
from .filesystem import detect_encoding, human_size, looks_like_text


class SkillToolset:
    """绑定 SkillsCatalog 的 ``loadSkill`` 实现。"""

    def __init__(
        self,
        catalog: SkillsCatalog,
        *,
        project_root: str | Path | None = None,
        workspace_root: str | Path | None = None,
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

    def registry_handlers(self) -> dict[str, Callable[..., str]]:
        return {"loadSkill": self.load_skill}

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
