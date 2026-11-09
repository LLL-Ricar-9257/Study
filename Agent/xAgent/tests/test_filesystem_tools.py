from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core import AgentLimits
from core.errors import ToolError
from core.tools.filesystem import FileToolset
from core.tools.registry import ToolRegistry
from core.tools.specs import FILE_TOOL_SPECS


@pytest.fixture
def toolset(tmp_path: Path) -> FileToolset:
    return FileToolset(tmp_path, limits=AgentLimits())


def test_all_file_tools(toolset: FileToolset, tmp_path: Path) -> None:
    assert str(tmp_path) in toolset.get_current_directory()
    datetime_info = toolset.get_current_datetime()
    assert "当前系统日期时间" in datetime_info
    assert "星期：" in datetime_info
    assert "UTC" in datetime_info

    assert "已创建目录" in toolset.create_directory("data/sub")
    assert "已创建文件" in toolset.create_file("data", "note.txt", "hello\nworld\n")

    listing = toolset.list_directory("data", recursive=True)
    assert "note.txt" in listing
    assert "sub/" in listing

    file_type = toolset.get_file_type("data/note.txt")
    assert "纯文本" in file_type
    assert "utf-8" in file_type

    content = toolset.read_text_file("data/note.txt")
    assert "hello" in content and "world" in content

    assert "已追加内容" in toolset.edit_text_file(
        "data/note.txt", "append", content="tail\n"
    )
    assert "已替换 1 处" in toolset.edit_text_file(
        "data/note.txt", "replace", old_text="hello", new_text="你好"
    )
    assert "你好" in toolset.read_text_file("data/note.txt")

    assert "已重命名文件" in toolset.rename_path("data/note.txt", "note2.txt")
    assert "已重命名目录" in toolset.rename_path("data/sub", "sub2")

    toolset.create_file("data", "delete-me.txt", "temporary")
    assert "已删除文件" in toolset.delete_file("data/delete-me.txt")
    assert "已删除目录" in toolset.delete_directory("data")


def test_workspace_boundary(toolset: FileToolset, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(ToolError):
        toolset.read_text_file("../outside.txt")


def test_create_file_requires_overwrite(toolset: FileToolset) -> None:
    toolset.create_file(".", "a.txt", "one")
    with pytest.raises(ToolError, match="文件已存在"):
        toolset.create_file(".", "a.txt", "two")
    assert "已覆盖文件" in toolset.create_file(".", "a.txt", "two", overwrite=True)


def test_cannot_delete_workspace_root(toolset: FileToolset) -> None:
    with pytest.raises(ToolError, match="不能删除"):
        toolset.delete_directory(".")


def test_registry_executes_and_reports_errors(toolset: FileToolset) -> None:
    registry = ToolRegistry(toolset.registry_handlers(), FILE_TOOL_SPECS)
    assert len(registry.schema()) == len(FILE_TOOL_SPECS)

    result = registry.execute("get_current_directory", "{}")
    assert result.ok
    assert "当前工作目录" in result.content

    assert not registry.execute("missing_tool", "{}").ok
    assert not registry.execute("list_directory", "not-json").ok
    assert not registry.execute("list_directory", '{"unknown": 1}').ok
    assert not registry.execute("list_directory", '{"path": "../"}').ok


def test_async_tools_are_executable_from_worker(toolset: FileToolset) -> None:
    async def call() -> str:
        return await asyncio.to_thread(toolset.get_current_directory)

    assert "当前工作目录" in asyncio.run(call())


@pytest.mark.parametrize(
    ("year", "leap"),
    [
        (2024, True),
        (2025, False),
        (1900, False),   # 整百年但不能被 400 整除
        (2000, True),    # 整百年且能被 400 整除
        (1600, True),
        (4, True),
        (1, False),
    ],
)
def test_is_leap_year_rules(toolset: FileToolset, year: int, leap: bool) -> None:
    """闰年规则必须包含「整百年需被 400 整除」这一条。"""
    output = toolset.is_leap_year(year)
    # 不能用 `"是闰年" in output`：非闰年的文案「不是闰年」也包含这个子串。
    verdict = output.split("\n判定规则", 1)[0]
    assert verdict.startswith(f"{year} 年是闰年") is leap
    assert f"{year} 年" in output
    assert "能被 400 整除" in output  # 规则本身要展示给用户


def test_is_leap_year_defaults_to_current_year(toolset: FileToolset) -> None:
    from datetime import datetime

    current = datetime.now().year
    output = toolset.is_leap_year()
    assert f"当前年份 {current}" in output
    assert f"{current} 年" in output


@pytest.mark.parametrize("bad", [0, -1, 10000, True])
def test_is_leap_year_rejects_out_of_range(toolset: FileToolset, bad) -> None:
    with pytest.raises(ToolError):
        toolset.is_leap_year(bad)


def test_is_leap_year_is_registered_as_tool(toolset: FileToolset) -> None:
    registry = ToolRegistry(toolset.registry_handlers(), FILE_TOOL_SPECS)
    names = {spec["function"]["name"] for spec in registry.schema()}

    assert "is_leap_year" in names
    assert len(names) == len(FILE_TOOL_SPECS)

    result = registry.execute("is_leap_year", '{"year": 2024}')
    assert result.ok
    assert "是闰年" in result.content

    # 参数错误必须作为工具失败回传给模型，而不是抛异常。
    assert not registry.execute("is_leap_year", '{"year": "abc"}').ok
    assert not registry.execute("is_leap_year", '{"unknown": 1}').ok
