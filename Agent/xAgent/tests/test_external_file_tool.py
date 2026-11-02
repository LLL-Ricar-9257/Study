from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import AgentLimits
from core.errors import ToolError
from core.tools.external import ExternalFileToolset
from core.tools.filesystem import FileToolset
from core.tools.registry import ToolRegistry
from core.tools.specs import ALL_TOOL_SPECS, EXTERNAL_TOOL_SPECS


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    root = tmp_path / "outside"
    root.mkdir()
    return root


@pytest.fixture
def toolset() -> ExternalFileToolset:
    return ExternalFileToolset(limits=AgentLimits())


def test_reads_file_outside_workspace(
    toolset: ExternalFileToolset, workspace: Path, outside: Path
) -> None:
    target = outside / "note.txt"
    target.write_text("OUTSIDE CONTENT\n第二行\n", encoding="utf-8")

    content = toolset.read_external_file(str(target))
    assert "读取通道：工作区外只读" in content
    assert "OUTSIDE CONTENT" in content
    assert "第二行" in content
    assert "安全提示" in content
    assert "utf-8" in content

    # 普通文件工具仍然被限制在工作区内，不因新工具而放宽。
    with pytest.raises(ToolError, match="不在工作区"):
        FileToolset(workspace).read_text_file(str(target))


def test_relative_path_resolves_against_cwd(
    toolset: ExternalFileToolset, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (outside / "note.txt").write_text("RELATIVE CONTENT", encoding="utf-8")
    monkeypatch.chdir(outside)

    assert "RELATIVE CONTENT" in toolset.read_external_file("note.txt")


def test_truncates_long_content(toolset: ExternalFileToolset, outside: Path) -> None:
    target = outside / "long.txt"
    target.write_text("a" * 100, encoding="utf-8")

    output = toolset.read_external_file(str(target), max_chars=10)
    assert "内容已截断" in output
    assert "前 10 个字符" in output


def test_rejects_oversized_file(outside: Path) -> None:
    target = outside / "big.txt"
    target.write_text("abcdef", encoding="utf-8")
    small = ExternalFileToolset(limits=AgentLimits(max_text_bytes=3))

    with pytest.raises(ToolError, match="过大"):
        small.read_external_file(str(target))


def test_rejects_missing_directory_and_binary(
    toolset: ExternalFileToolset, outside: Path
) -> None:
    with pytest.raises(ToolError, match="文件不存在"):
        toolset.read_external_file(str(outside / "missing.txt"))
    with pytest.raises(ToolError, match="目录"):
        toolset.read_external_file(str(outside))

    binary = outside / "binary.bin"
    binary.write_bytes(b"\x00\x01\x02\x03")
    with pytest.raises(ToolError, match="不是纯文本"):
        toolset.read_external_file(str(binary))


def test_rejects_invalid_paths(toolset: ExternalFileToolset) -> None:
    with pytest.raises(ToolError, match="不能为空"):
        toolset.read_external_file("   ")
    with pytest.raises(ToolError, match="空字符"):
        toolset.read_external_file("bad\x00path")
    with pytest.raises(ToolError, match="设备"):
        toolset.read_external_file("\\\\.\\pipe\\x")


def test_reads_empty_and_non_utf8_text(
    toolset: ExternalFileToolset, outside: Path
) -> None:
    empty = outside / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert "（空文件）" in toolset.read_external_file(str(empty))

    gbk = outside / "gbk.txt"
    gbk.write_bytes("中文内容".encode("gb18030"))
    output = toolset.read_external_file(str(gbk))
    assert "中文内容" in output
    assert "gb18030" in output


def test_registered_for_llm(outside: Path) -> None:
    toolset = ExternalFileToolset()
    registry = ToolRegistry(toolset.registry_handlers(), EXTERNAL_TOOL_SPECS)

    assert [spec["function"]["name"] for spec in registry.schema()] == [
        "readExternalFile"
    ]
    assert "readExternalFile" in {spec.name for spec in ALL_TOOL_SPECS}

    schema = registry.schema()[0]["function"]["parameters"]
    assert schema["required"] == ["path"]
    assert set(schema["properties"]) == {"path", "max_chars"}

    target = outside / "note.txt"
    target.write_text("REGISTRY CONTENT", encoding="utf-8")
    result = registry.execute(
        "readExternalFile",
        json.dumps({"path": str(target)}),
    )
    assert result.ok
    assert "REGISTRY CONTENT" in result.content

    missing = registry.execute("readExternalFile", '{"path": ""}')
    assert not missing.ok
    assert "不能为空" in missing.content

    bad_limit = registry.execute(
        "readExternalFile",
        json.dumps({"path": str(target), "max_chars": "many"}),
    )
    assert not bad_limit.ok
    assert "max_chars" in bad_limit.content
