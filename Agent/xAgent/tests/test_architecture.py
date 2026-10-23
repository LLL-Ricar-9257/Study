from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_core_does_not_import_presentation_layers() -> None:
    forbidden = re.compile(r"^\s*(?:from|import)\s+(?:textual|tui|rich)\b")
    offenders: list[str] = []
    for path in (PROJECT_ROOT / "core").rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if forbidden.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {line.strip()}")
    assert not offenders, "core 不得依赖 UI 库：\n" + "\n".join(offenders)


def test_core_only_depends_on_abstract_model_port() -> None:
    agent_source = (PROJECT_ROOT / "core" / "agent.py").read_text(encoding="utf-8")
    assert "openai" not in agent_source
    assert "ChatModel" in agent_source


def test_ui_event_contract_is_public() -> None:
    import core

    for name in (
        "AgentEvent",
        "TextDelta",
        "ToolStarted",
        "ToolFinished",
        "TurnCompleted",
        "TurnFailed",
    ):
        assert hasattr(core, name)
