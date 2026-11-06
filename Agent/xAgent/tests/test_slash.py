from __future__ import annotations

from tui.slash import (
    SLASH_COMMANDS,
    build_template,
    filter_commands,
    find_command,
    help_lines,
)


def test_find_command_supports_aliases_and_slash_prefix() -> None:
    assert find_command("/compact").name == "compact"  # type: ignore[union-attr]
    assert find_command("summarize").name == "compact"  # type: ignore[union-attr]
    assert find_command("polish").name == "refine"  # type: ignore[union-attr]
    assert find_command("/new").name == "new"  # type: ignore[union-attr]
    assert find_command("sessions").name == "resume"  # type: ignore[union-attr]
    assert find_command("does-not-exist") is None


def test_filter_commands_prefers_exact_name_over_keywords() -> None:
    results = filter_commands("context")
    assert results[0].name == "context"
    assert all("context" in command.name or "context" in command.description
               or "context" in " ".join(command.keywords)
               for command in results)


def test_filter_commands_empty_query_returns_all() -> None:
    assert filter_commands("") == SLASH_COMMANDS


def test_filter_commands_matches_chinese_keywords() -> None:
    names = {command.name for command in filter_commands("压缩")}
    assert "compact" in names


def test_templates_and_help_are_generated() -> None:
    review = find_command("review")
    assert review is not None
    text = build_template(review, "重点看安全")
    assert text is not None
    assert "请审查当前工作区" in text
    assert "本次重点关注：重点看安全" in text

    init = find_command("init")
    assert init is not None
    init_text = build_template(init, "")
    assert init_text is not None
    assert "AGENTS.md" in init_text
    assert build_template(find_command("help"), "") is None  # type: ignore[arg-type]

    help_text = help_lines()
    assert "/compact" in help_text
    assert "/refine" in help_text
