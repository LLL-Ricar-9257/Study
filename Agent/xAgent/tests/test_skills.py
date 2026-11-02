from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core import AgentLimits, AppConfig, LLMSettings
from core.bootstrap import build_agent
from core.errors import ToolError
from core.prompts import build_system_prompt
from core.skills import SkillsCatalog, bundled_root, normalize_skill_name
from core.tools.registry import ToolRegistry
from core.tools.skills import SkillToolset
from core.tools.specs import ALL_TOOL_SPECS, SKILL_TOOL_SPECS


def _make_skill(
    root: Path,
    directory: str,
    *,
    name: str = "demo",
    description: str = "Demo skill.",
    body: str = "# Instructions\n\nFollow these steps.",
    files: dict[str, str] | None = None,
) -> Path:
    skill_dir = root / ".agents" / "skills" / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    for relative, content in (files or {}).items():
        target = skill_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return skill_dir


@pytest.fixture
def catalog(tmp_path: Path) -> SkillsCatalog:
    _make_skill(
        tmp_path,
        "demo",
        name="demo-skill",
        description="Functions that demo has.",
        body="UNIQUE BODY MARKER",
        files={
            "references/guide.md": "GUIDE CONTENT",
            "docs/guide.md": "OTHER GUIDE CONTENT",
            "references/nested/tips.txt": "TIPS CONTENT",
        },
    )
    return SkillsCatalog.discover(tmp_path)


def test_discovery_reads_only_name_and_description(tmp_path: Path) -> None:
    _make_skill(
        tmp_path,
        "pdf",
        name="pdf-tools",
        description="Extract text and tables from PDF files.",
        body="SECRET FULL INSTRUCTIONS",
    )
    _make_skill(
        tmp_path,
        "quoted",
        name="quoted-skill",
        description='"Quoted description with: colon."',
        body="quoted body",
    )

    discovered = SkillsCatalog.discover(tmp_path)
    assert [skill.name for skill in discovered] == ["pdf-tools", "quoted-skill"]
    assert discovered.get("pdf-tools") is not None
    assert discovered.get("PDF-TOOLS") is not None
    assert discovered.get("quoted-skill").description == "Quoted description with: colon."

    prompt = build_system_prompt(tmp_path, discovered)
    assert "You have access to the following skills:" in prompt
    assert "- 'pdf-tools': Extract text and tables from PDF files." in prompt
    assert "Use the 'loadSkill' tool" in prompt
    assert "SECRET FULL INSTRUCTIONS" not in prompt


def test_multiline_frontmatter_is_parsed(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".agents" / "skills" / "folded"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: folded\n"
        "description: >\n"
        "  First line of the description\n"
        "  and the second line.\n"
        "---\n"
        "Body.\n",
        encoding="utf-8",
    )

    discovered = SkillsCatalog.discover(tmp_path)
    assert discovered.skills[0].description == (
        "First line of the description and the second line."
    )


def test_invalid_and_duplicate_skills_are_skipped(tmp_path: Path) -> None:
    _make_skill(tmp_path, "good", name="good", description="Good.")
    _make_skill(tmp_path, "duplicate", name="good", description="Duplicate.")
    _make_skill(tmp_path, "no-description", name="no-description", description="")
    (tmp_path / ".agents" / "skills" / "missing-file").mkdir(parents=True)

    discovered = SkillsCatalog.discover(tmp_path)
    assert [skill.name for skill in discovered] == ["good"]
    assert any("description" in issue for issue in discovered.issues)
    assert any("缺少 SKILL.md" in issue for issue in discovered.issues)


def test_missing_skills_directory_is_empty(tmp_path: Path) -> None:
    discovered = SkillsCatalog.discover(tmp_path)
    assert len(discovered) == 0
    assert "You have access to the following skills:" not in build_system_prompt(
        tmp_path, discovered
    )


def test_load_skill_returns_full_skill_md(catalog: SkillsCatalog, tmp_path: Path) -> None:
    result = SkillToolset(catalog).load_skill("demo-skill")
    assert "Skill：demo-skill" in result
    assert "类型：SKILL.md" in result
    assert "UNIQUE BODY MARKER" in result
    assert "name: demo-skill" in result


def test_load_attachment_by_skill_relative_path(
    catalog: SkillsCatalog, tmp_path: Path
) -> None:
    toolset = SkillToolset(catalog)
    result = toolset.load_skill("demo-skill", "references/guide.md")
    assert "类型：附件" in result
    assert "GUIDE CONTENT" in result
    assert "OTHER GUIDE CONTENT" not in result

    nested = toolset.load_skill("demo-skill", "./references/nested/tips.txt")
    assert "TIPS CONTENT" in nested


def test_load_attachment_by_project_relative_path(
    catalog: SkillsCatalog, tmp_path: Path
) -> None:
    result = SkillToolset(catalog, project_root=tmp_path).load_skill(
        "demo-skill",
        ".agents/skills/demo/references/guide.md",
    )
    assert "GUIDE CONTENT" in result


def test_load_attachment_by_absolute_path(
    catalog: SkillsCatalog, tmp_path: Path
) -> None:
    absolute = (tmp_path / ".agents" / "skills" / "demo" / "docs" / "guide.md").resolve()
    result = SkillToolset(catalog).load_skill("demo-skill", str(absolute))
    assert "OTHER GUIDE CONTENT" in result


def test_unique_attachment_filename_is_found_recursively(
    catalog: SkillsCatalog,
) -> None:
    result = SkillToolset(catalog).load_skill("demo-skill", "tips.txt")
    assert "TIPS CONTENT" in result


def test_ambiguous_attachment_returns_candidates(catalog: SkillsCatalog) -> None:
    result = SkillToolset(catalog).load_skill("demo-skill", "guide.md")
    assert "多个名为 'guide.md'" in result
    assert "- docs/guide.md" in result
    assert "- references/guide.md" in result
    assert "GUIDE CONTENT" not in result


def test_attachment_cannot_escape_skill_directory(
    catalog: SkillsCatalog, tmp_path: Path
) -> None:
    (tmp_path / "secret.txt").write_text("SECRET", encoding="utf-8")
    toolset = SkillToolset(catalog)

    with pytest.raises(ToolError, match="Skill 目录之外"):
        toolset.load_skill("demo-skill", "../../../secret.txt")
    with pytest.raises(ToolError, match="Skill 目录之外"):
        toolset.load_skill("demo-skill", str((tmp_path / "secret.txt").resolve()))


def test_cannot_read_files_of_another_skill(tmp_path: Path) -> None:
    _make_skill(tmp_path, "first", name="first")
    _make_skill(tmp_path, "second", name="second")
    catalog = SkillsCatalog.discover(tmp_path)
    toolset = SkillToolset(catalog, project_root=tmp_path)

    with pytest.raises(ToolError, match="Skill 目录之外"):
        toolset.load_skill("first", ".agents/skills/second/SKILL.md")

    # 精确路径仍然可以读取 first 自己的文件。
    assert "name: first" in toolset.load_skill("first")


def test_unknown_skill_is_rejected(catalog: SkillsCatalog) -> None:
    with pytest.raises(ToolError, match="未找到 Skill"):
        SkillToolset(catalog).load_skill("nope")


def test_missing_attachment_is_rejected(catalog: SkillsCatalog) -> None:
    with pytest.raises(ToolError, match="找不到附件"):
        SkillToolset(catalog).load_skill("demo-skill", "references/nope.md")


def test_load_skill_is_registered_for_llm(catalog: SkillsCatalog) -> None:
    toolset = SkillToolset(catalog)
    registry = ToolRegistry(toolset.registry_handlers(), SKILL_TOOL_SPECS)

    assert [spec["function"]["name"] for spec in registry.schema()] == [
        "loadSkill",
        "createSkill",
    ]
    parameters = registry.schema()[0]["function"]["parameters"]
    assert parameters["required"] == ["name"]
    assert set(parameters["properties"]) == {"name", "filename"}

    result = registry.execute("loadSkill", '{"name": "demo-skill"}')
    assert result.ok
    assert "UNIQUE BODY MARKER" in result.content

    attachment = registry.execute(
        "loadSkill",
        '{"name": "demo-skill", "filename": "references/guide.md"}',
    )
    assert attachment.ok
    assert "GUIDE CONTENT" in attachment.content


def test_bootstrap_injects_discovered_skills(tmp_path: Path) -> None:
    _make_skill(tmp_path, "demo", name="demo-skill", description="Demo description.")
    config = AppConfig(
        workspace=tmp_path,
        llm=LLMSettings(api_key="test-key"),
        limits=AgentLimits(),
    )

    agent = build_agent(config, project_root=tmp_path)
    assert "demo-skill" in agent.messages[0].content
    assert "Demo description." in agent.messages[0].content
    asyncio.run(agent.aclose())


def test_workspace_fallback_and_directory_alias(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    _make_skill(
        workspace,
        "notice",
        name="撰写通知",
        description="撰写公司内部通知",
        body="NOTICE INSTRUCTIONS",
    )

    catalog = SkillsCatalog.discover(project, extra_roots=(workspace,))
    assert [skill.name for skill in catalog.skills] == ["撰写通知"]
    assert catalog.get("notice") is not None
    assert catalog.get("撰写通知") is not None

    result = SkillToolset(
        catalog, project_root=project, workspace_root=workspace
    ).load_skill("notice")
    assert "NOTICE INSTRUCTIONS" in result


def test_bootstrap_merges_startup_and_workspace_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    _make_skill(project, "root-skill", name="root-skill", description="From cwd.")
    _make_skill(
        workspace,
        "workspace-skill",
        name="workspace-skill",
        description="From workspace.",
    )

    monkeypatch.chdir(project)
    config = AppConfig(
        workspace=workspace,
        llm=LLMSettings(api_key="test-key"),
        limits=AgentLimits(),
    )

    agent = build_agent(config)
    assert "root-skill" in agent.messages[0].content
    assert "workspace-skill" in agent.messages[0].content
    asyncio.run(agent.aclose())


def test_bootstrap_uses_startup_cwd_for_skill_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _make_skill(project, "demo", name="cwd-skill", description="From cwd.")

    monkeypatch.chdir(project)
    config = AppConfig(
        workspace=workspace,
        llm=LLMSettings(api_key="test-key"),
        limits=AgentLimits(),
    )

    agent = build_agent(config)
    assert "cwd-skill" in agent.messages[0].content
    asyncio.run(agent.aclose())


def test_bundled_skills_are_scanned_at_lowest_priority(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    bundled = tmp_path / "bundled"
    project.mkdir()
    workspace.mkdir()

    _make_skill(project, "shared", name="shared", description="From project.")
    _make_skill(workspace, "shared", name="shared", description="From workspace.")
    _make_skill(bundled, "shared", name="shared", description="From bundled.")
    _make_skill(bundled, "extra", name="extra-skill", description="From bundled.")

    catalog = SkillsCatalog.discover(
        project,
        extra_roots=(workspace,),
        bundled_roots=(bundled,),
    )

    assert catalog.get("shared") is not None
    assert catalog.get("shared").description == "From project."
    assert [skill.name for skill in catalog.skills] == ["shared", "extra-skill"]
    assert catalog.searched_roots == (
        project.resolve(),
        workspace.resolve(),
        bundled.resolve(),
    )
    # 自带目录里的 Skill 可以正常加载，附件解析也以其自身目录为边界。
    assert "From bundled." in SkillToolset(catalog).load_skill("extra-skill")


def test_duplicate_bundled_root_is_scanned_once(tmp_path: Path) -> None:
    _make_skill(tmp_path, "demo", name="demo", description="Demo.")

    catalog = SkillsCatalog.discover(tmp_path, bundled_roots=(tmp_path,))

    assert [skill.name for skill in catalog.skills] == ["demo"]
    assert catalog.issues == ()
    assert catalog.searched_roots == (tmp_path.resolve(),)


def test_bundled_root_points_into_the_source_tree() -> None:
    root = bundled_root()

    assert root.resolve() == Path(__file__).resolve().parent.parent
    assert (root / ".agents" / "skills").is_dir()
    catalog = SkillsCatalog.discover(root, bundled_roots=(root,))
    # 自带根与启动目录重合时只扫描一次，不会把内置 Skill 当成重复项跳过。
    assert catalog.searched_roots == (root.resolve(),)
    assert catalog.issues == ()


def test_bootstrap_includes_bundled_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    bundled = tmp_path / "bundled"
    project.mkdir()
    workspace.mkdir()
    _make_skill(bundled, "builtin", name="builtin-skill", description="Bundled.")

    monkeypatch.chdir(project)
    config = AppConfig(
        workspace=workspace,
        llm=LLMSettings(api_key="test-key"),
        limits=AgentLimits(),
    )

    agent = build_agent(config, bundled_roots=(bundled,))
    assert "builtin-skill" in agent.messages[0].content
    assert "Bundled." in agent.messages[0].content
    asyncio.run(agent.aclose())


def test_bootstrap_can_disable_bundled_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    _make_skill(project, "demo", name="cwd-skill", description="From cwd.")

    monkeypatch.chdir(project)
    config = AppConfig(
        workspace=workspace,
        llm=LLMSettings(api_key="test-key"),
        limits=AgentLimits(),
    )

    agent = build_agent(config, bundled_roots=())
    assert "cwd-skill" in agent.messages[0].content
    assert "撰写通知" not in agent.messages[0].content
    asyncio.run(agent.aclose())

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PDF Tools", "pdf-tools"),
        ("pdf_tools", "pdf-tools"),
        ("  My--Skill  ", "my-skill"),
        ("skill.v2", "skill-v2"),
        ("Café Tools", "cafe-tools"),
        ("A1", "a1"),
    ],
)
def test_normalize_skill_name(raw: str, expected: str) -> None:
    assert normalize_skill_name(raw) == expected


def test_create_skill_writes_valid_skill_md(tmp_path: Path) -> None:
    catalog = SkillsCatalog.discover(tmp_path)
    result = SkillToolset(catalog).create_skill(
        "PDF Tools",
        "---\n"
        "name: pdf-tools\n"
        "description: Extract text and tables from PDF files.\n"
        "license: MIT\n"
        "---\n\n"
        "# PDF Tools\n\n"
        "UNIQUE BODY MARKER\n",
    )

    skill_dir = tmp_path / ".agents" / "skills" / "pdf-tools"
    assert skill_dir.is_dir()
    assert sorted(entry.name for entry in skill_dir.iterdir()) == ["SKILL.md"]
    assert "已创建 Skill：pdf-tools" in result
    assert "create_directory" in result

    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: pdf-tools\ndescription: ")
    assert "license: MIT" in text
    assert text.endswith("UNIQUE BODY MARKER\n")

    discovered = SkillsCatalog.discover(tmp_path)
    assert discovered.issues == ()
    skill = discovered.get("pdf-tools")
    assert skill is not None
    assert skill.description == "Extract text and tables from PDF files."
    assert "UNIQUE BODY MARKER" in SkillToolset(discovered).load_skill("pdf-tools")


def test_create_skill_generates_frontmatter_when_missing(tmp_path: Path) -> None:
    catalog = SkillsCatalog.discover(tmp_path)
    SkillToolset(catalog).create_skill(
        "Weekly_Report",
        "# 周报生成\n\n按模板汇总本周进展。\n",
    )

    discovered = SkillsCatalog.discover(tmp_path)
    skill = discovered.get("weekly-report")
    assert skill is not None
    assert skill.description == "周报生成"
    assert "按模板汇总本周进展。" in SkillToolset(discovered).load_skill("weekly-report")


def test_create_skill_quotes_ambiguous_description(tmp_path: Path) -> None:
    catalog = SkillsCatalog.discover(tmp_path)
    SkillToolset(catalog).create_skill(
        "debugging",
        "---\n"
        "name: debugging\n"
        'description: Fix errors: "quoted" and more.\n'
        "---\n\n"
        "# Debugging\n",
    )

    skill = SkillsCatalog.discover(tmp_path).get("debugging")
    assert skill is not None
    assert skill.description == 'Fix errors: "quoted" and more.'


def test_create_skill_refuses_to_overwrite_registered_skill(tmp_path: Path) -> None:
    _make_skill(tmp_path, "demo", name="demo", description="Original.")
    skill_file = tmp_path / ".agents" / "skills" / "demo" / "SKILL.md"
    original = skill_file.read_text(encoding="utf-8")
    toolset = SkillToolset(SkillsCatalog.discover(tmp_path))

    with pytest.raises(ToolError, match="已存在"):
        toolset.create_skill(
            "demo",
            "---\nname: demo\ndescription: New.\n---\n\nNEW BODY\n",
        )

    assert skill_file.read_text(encoding="utf-8") == original


def test_create_skill_respects_directory_alias(tmp_path: Path) -> None:
    _make_skill(tmp_path, "demo", name="demo-skill", description="Original.")
    toolset = SkillToolset(SkillsCatalog.discover(tmp_path))

    with pytest.raises(ToolError, match="已存在"):
        toolset.create_skill(
            "demo",
            "---\nname: demo\ndescription: Alias conflict.\n---\n\nBODY\n",
        )
    assert not (tmp_path / ".agents" / "skills" / "demo-skill" / "demo").exists()


def test_create_skill_refuses_existing_directory(tmp_path: Path) -> None:
    empty_dir = tmp_path / ".agents" / "skills" / "taken"
    empty_dir.mkdir(parents=True)
    toolset = SkillToolset(SkillsCatalog.discover(tmp_path))

    with pytest.raises(ToolError, match="已存在"):
        toolset.create_skill(
            "taken",
            "---\nname: taken\ndescription: Taken.\n---\n\nBODY\n",
        )

    assert list(empty_dir.iterdir()) == []


def test_create_skill_rejects_invalid_or_unsafe_input(tmp_path: Path) -> None:
    toolset = SkillToolset(SkillsCatalog.discover(tmp_path))

    with pytest.raises(ToolError, match="英文名称"):
        toolset.create_skill("撰写通知", "# 正文\n")
    with pytest.raises(ToolError, match="路径分隔符"):
        toolset.create_skill("../evil", "# 正文\n")
    with pytest.raises(ToolError, match="不能为空"):
        toolset.create_skill("", "# 正文\n")
    with pytest.raises(ToolError, match="不能为空"):
        toolset.create_skill("demo", "   ")

    assert not (tmp_path / ".agents").exists()


def test_create_skill_validates_frontmatter_and_body(tmp_path: Path) -> None:
    toolset = SkillToolset(SkillsCatalog.discover(tmp_path))

    with pytest.raises(ToolError, match="不一致"):
        toolset.create_skill(
            "alpha",
            "---\nname: beta\ndescription: Mismatch.\n---\n\nBODY\n",
        )
    with pytest.raises(ToolError, match="缺少 description"):
        toolset.create_skill("alpha", "---\nname: alpha\n---\n\n")
    with pytest.raises(ToolError, match="缺少正文"):
        toolset.create_skill(
            "alpha",
            "---\nname: alpha\ndescription: No body.\n---\n",
        )
    with pytest.raises(ToolError, match="结束分隔符"):
        toolset.create_skill(
            "alpha",
            "---\nname: alpha\ndescription: Unterminated.\n",
        )

    assert not (tmp_path / ".agents" / "skills" / "alpha").exists()


def test_create_skill_is_registered_for_llm(tmp_path: Path) -> None:
    toolset = SkillToolset(SkillsCatalog.discover(tmp_path))
    registry = ToolRegistry(toolset.registry_handlers(), SKILL_TOOL_SPECS)

    assert "createSkill" in {spec.name for spec in ALL_TOOL_SPECS}
    schema = next(
        item["function"]["parameters"]
        for item in registry.schema()
        if item["function"]["name"] == "createSkill"
    )
    assert schema["required"] == ["name", "skillContext"]
    assert set(schema["properties"]) == {"name", "skillContext"}

    arguments = json.dumps(
        {
            "name": "Demo Skill",
            "skillContext": (
                "---\nname: demo-skill\ndescription: Demo skill.\n---\n\n# Demo\n"
            ),
        }
    )
    result = registry.execute("createSkill", arguments)
    assert result.ok
    assert "已创建 Skill：demo-skill" in result.content

    duplicate = registry.execute("createSkill", arguments)
    assert not duplicate.ok
    assert "已存在" in duplicate.content

def test_create_skill_preserves_extra_frontmatter_fields(tmp_path: Path) -> None:
    catalog = SkillsCatalog.discover(tmp_path)
    SkillToolset(catalog).create_skill(
        "pdf-tools",
        "---\n"
        "name: pdf-tools\n"
        "description: PDF tools.\n"
        "license: MIT\n"
        "metadata:\n"
        "  author: xagent\n"
        "  version: 1.0\n"
        "allowed-tools: [Read, Write]\n"
        "---\n\n"
        "# PDF Tools\n",
    )

    text = (
        tmp_path / ".agents" / "skills" / "pdf-tools" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "license: MIT\n" in text
    assert "metadata:\n  author: xagent\n  version: 1.0\n" in text
    assert "allowed-tools: [Read, Write]\n" in text
    # 只重写 name/description，原字段不会重复出现。
    assert text.count("name:") == 1
    assert text.count("description:") == 1
    assert SkillsCatalog.discover(tmp_path).issues == ()
