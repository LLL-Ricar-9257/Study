from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from core.errors import ToolError
from core.tools.registry import ToolRegistry
from core.tools.runcli import RunCliToolset
from core.tools.specs import (
    ALL_TOOL_SPECS,
    FILE_TOOL_SPECS,
    RUNCLI_TOOL_SPECS,
    SEARCH_TOOL_SPECS,
    WEB_TOOL_SPECS,
)


def _completed(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["command"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patch_run(
    monkeypatch: pytest.MonkeyPatch, result: Any
) -> list[tuple[list[str], dict[str, Any]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        calls.append((command, kwargs))
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_runcli_executes_argv_without_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_run(
        monkeypatch,
        _completed(stdout=b"hello\n", stderr=b"warn\n"),
    )
    toolset = RunCliToolset(tmp_path)

    output = toolset.run_cli("git", ["status", "--short"])

    command, kwargs = calls[0]
    assert command == ["git", "status", "--short"]
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == str(tmp_path.resolve())
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == 30.0
    assert "退出码：0" in output
    assert "执行状态：成功" in output
    assert "hello" in output
    assert "warn" in output


def test_runcli_returns_nonzero_exit_and_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(
        monkeypatch,
        _completed(stderr=b"fatal: not a git repository\n", returncode=128),
    )

    output = RunCliToolset(tmp_path).run_cli("git", ["status"])

    assert "退出码：128" in output
    assert "执行状态：失败（非零退出码）" in output
    assert "fatal: not a git repository" in output


def test_runcli_reports_timeout_with_partial_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(
        monkeypatch,
        subprocess.TimeoutExpired(
            cmd=["slow"],
            timeout=30,
            output=b"partial stdout",
            stderr=b"partial stderr",
        ),
    )

    output = RunCliToolset(tmp_path).run_cli("slow")

    assert "退出码：不可用" in output
    assert "超时" in output
    assert "partial stdout" in output
    assert "partial stderr" in output


def test_runcli_truncates_long_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, _completed(stdout=("x" * 500).encode()))

    output = RunCliToolset(tmp_path, max_output_chars=100).run_cli("verbose")

    assert "输出过长已截断" in output
    assert "仅显示前 100 字符" in output
    assert "另有 400 个字符未显示" in output
    assert output.count("x") == 100


def test_runcli_reports_missing_program(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, FileNotFoundError())

    with pytest.raises(ToolError, match="找不到命令行程序"):
        RunCliToolset(tmp_path).run_cli("not-installed")


def test_runcli_rejects_invalid_arguments(tmp_path: Path) -> None:
    toolset = RunCliToolset(tmp_path)

    with pytest.raises(ToolError, match="args 必须是字符串列表"):
        toolset.run_cli("git", "status")  # type: ignore[arg-type]
    with pytest.raises(ToolError, match=r"args\[1\] 必须是字符串"):
        toolset.run_cli("git", ["status", ["nested"]])  # type: ignore[list-item]
    with pytest.raises(ToolError, match="command 不能为空"):
        toolset.run_cli("   ")


def test_runcil_is_registered_for_llm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, _completed(stdout=b"ok"))
    toolset = RunCliToolset(tmp_path)
    registry = ToolRegistry(toolset.registry_handlers(), RUNCLI_TOOL_SPECS)

    assert [spec["function"]["name"] for spec in registry.schema()] == ["Runcil"]
    assert "Runcil" in {spec.name for spec in ALL_TOOL_SPECS}
    assert len(ALL_TOOL_SPECS) == (
        len(FILE_TOOL_SPECS)
        + len(WEB_TOOL_SPECS)
        + len(SEARCH_TOOL_SPECS)
        + len(RUNCLI_TOOL_SPECS)
    )
    schema = registry.schema()[0]["function"]["parameters"]
    assert schema["required"] == ["command"]
    assert set(schema["properties"]) == {"command", "args"}

    result = registry.execute(
        "Runcil", '{"command": "git", "args": ["status", "--short"]}'
    )
    assert result.ok
    assert "退出码：0" in result.content


def test_runcil_registry_reports_invalid_arguments(tmp_path: Path) -> None:
    registry = ToolRegistry(
        RunCliToolset(tmp_path).registry_handlers(), RUNCLI_TOOL_SPECS
    )

    result = registry.execute("Runcil", "{}")

    assert not result.ok
    assert "参数不匹配" in result.content


def test_runcli_runs_real_process(tmp_path: Path) -> None:
    script = (
        "import sys; "
        "print('hello from subprocess'); "
        "print('oops', file=sys.stderr); "
        "sys.exit(3)"
    )

    output = RunCliToolset(tmp_path).run_cli(sys.executable, ["-c", script])

    assert "退出码：3" in output
    assert "hello from subprocess" in output
    assert "oops" in output
