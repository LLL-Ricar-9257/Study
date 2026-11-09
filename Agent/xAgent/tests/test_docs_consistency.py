"""文档一致性：README / 系统提示与代码里的工具清单必须双向对齐。

这类测试解决的是"文档写了但代码没有"和"代码加了但文档没写"两类漂移：

* 每个 :data:`ALL_TOOL_SPECS` 里的工具名都必须出现在 ``README.md``；
* ``README.md`` 里声明的工具总数必须等于 ``len(ALL_TOOL_SPECS)``；
* 系统提示词里点名的工具必须是已注册的工具；
* 文档里不得残留已删除的工具名。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.prompts import build_system_prompt
from core.tools.specs import ALL_TOOL_SPECS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
README = PROJECT_ROOT / "README.md"
CONFIG_EXAMPLE = PROJECT_ROOT / "config.example.ini"

SPEC_NAMES = tuple(spec.name for spec in ALL_TOOL_SPECS)

REMOVED_TOOL_NAMES = ("get_first_workspace_files", "readFile", "writeFile")


def readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_every_registered_tool_is_documented() -> None:
    text = readme_text()
    missing = [name for name in SPEC_NAMES if name not in text]
    assert not missing, f"README 未提到这些工具：{missing}"


def test_readme_declares_the_real_tool_count() -> None:
    """README 里 ``共 N 个工具`` 必须与 ALL_TOOL_SPECS 一致。"""
    match = re.search(r"共\s*(\d+)\s*个工具", readme_text())
    assert match, "README 缺少 `共 N 个工具` 声明，无法校验数量"
    assert int(match.group(1)) == len(SPEC_NAMES)


@pytest.mark.parametrize("name", REMOVED_TOOL_NAMES)
def test_readme_has_no_removed_tool(name: str) -> None:
    assert name not in readme_text(), f"README 仍提到已移除的工具 {name}"


def test_specs_have_unique_names_and_object_schema() -> None:
    assert len(set(SPEC_NAMES)) == len(SPEC_NAMES)
    for spec in ALL_TOOL_SPECS:
        schema = spec.as_openai_schema()
        assert schema["function"]["name"] == spec.name
        assert schema["function"]["parameters"]["type"] == "object"
        assert schema["function"]["description"], spec.name


def test_system_prompt_only_names_registered_tools(tmp_path: Path) -> None:
    prompt = build_system_prompt(tmp_path)
    mentioned = {
        name
        for name in SPEC_NAMES + REMOVED_TOOL_NAMES
        if name in prompt
    }
    assert mentioned <= set(SPEC_NAMES), f"提示词点名了未注册的工具：{mentioned - set(SPEC_NAMES)}"
    # 时间 / 闰年两个工具必须在规则里被点名，否则模型不会调用。
    assert "get_current_datetime" in prompt
    assert "is_leap_year" in prompt


def test_config_example_covers_every_section_used_in_code() -> None:
    text = CONFIG_EXAMPLE.read_text(encoding="utf-8")
    for section in (
        "[llm]",
        "[agent]",
        "[context]",
        "[sessions]",
        "[tui]",
        "[tools]",
        "[search]",
        "[mcp]",
        "[logs]",
    ):
        assert section in text, f"config.example.ini 缺少 {section}"
