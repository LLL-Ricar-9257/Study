from __future__ import annotations

import subprocess
from typing import Any

import pytest

from core.config import AgentLimits
from core.errors import ToolError
from core.tools.registry import ToolRegistry
from core.tools.specs import (
    ALL_TOOL_SPECS,
    FILE_TOOL_SPECS,
    RUNCLI_TOOL_SPECS,
    SEARCH_TOOL_SPECS,
    WEB_TOOL_SPECS,
)
from core.tools.web import WebToolset

URL = "https://example.com/article"


def _html() -> str:
    paragraphs = "".join(
        f"<p>第 {index} 段正文，包含<a href='https://example.com/{index}'>链接</a>，"
        "以及足够长的内容用于验证正文提取效果。Lorem ipsum dolor sit amet, "
        "consectetur adipiscing elit, sed do eiusmod tempor incididunt.</p>"
        for index in range(12)
    )
    return f"""<html><head><title>测试标题</title></head><body>
<nav>首页 | 关于 | 联系</nav>
<article>
<h1>测试标题</h1>
{paragraphs}
<h2>小节</h2>
<ul><li>第一项</li><li>第二项</li></ul>
<table><tr><th>名称</th><th>数值</th></tr><tr><td>甲</td><td>1</td></tr></table>
</article>
<footer>页脚版权信息</footer>
</body></html>"""


def _completed(
    body: bytes,
    *,
    status: str = "200",
    content_type: str = "text/html; charset=utf-8",
    returncode: int = 0,
    stderr: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    stdout = body + b"\nXAGENT-CURL-META\t" + f"{status}\t{content_type}".encode()
    return subprocess.CompletedProcess(
        args=["curl"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patch_run(
    monkeypatch: pytest.MonkeyPatch, result: Any
) -> list[tuple[list[str], dict[str, Any]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        calls.append((command, kwargs))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_fetch_web_returns_markdown_with_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch, _completed(_html().encode()))
    toolset = WebToolset(limits=AgentLimits(), curl_path="curl")

    output = toolset.fetch_web(URL)

    assert URL in output
    assert "测试标题" in output
    assert "# 测试标题" in output
    assert "[链接](https://example.com/1)" in output
    assert "- 第一项" in output
    assert "| 名称 | 数值 |" in output
    assert "页脚版权信息" not in output

    command, kwargs = calls[0]
    assert isinstance(command, list)
    assert command[-1] == URL
    assert "--" in command
    assert kwargs["shell"] is False


def test_fetch_web_rejects_non_http_url() -> None:
    toolset = WebToolset(curl_path="curl")
    for url in ("", "ftp://example.com/a", "file:///etc/passwd", "example.com"):
        with pytest.raises(ToolError):
            toolset.fetch_web(url)


def test_fetch_web_reports_curl_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(
        monkeypatch,
        _completed(
            b"",
            status="000",
            content_type="",
            returncode=7,
            stderr=b"Could not resolve host",
        ),
    )
    with pytest.raises(ToolError, match="无法连接到网页服务器"):
        WebToolset(curl_path="curl").fetch_web(URL)


def test_fetch_web_reports_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, subprocess.TimeoutExpired(cmd="curl", timeout=25))
    with pytest.raises(ToolError, match="超时"):
        WebToolset(curl_path="curl", timeout_seconds=20).fetch_web(URL)


def test_fetch_web_requires_curl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.tools.web.shutil.which", lambda _name: None)
    with pytest.raises(ToolError, match="curl"):
        WebToolset().fetch_web(URL)


def test_fetch_web_rejects_non_html_content(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, _completed(b"%PDF-1.7", content_type="application/pdf"))
    with pytest.raises(ToolError, match="不是 HTML"):
        WebToolset(curl_path="curl").fetch_web(URL)


def test_fetch_web_reports_parse_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, _completed(b"<html></html>"))
    monkeypatch.setattr("trafilatura.load_html", lambda _body: None)
    with pytest.raises(ToolError, match="HTML 解析失败"):
        WebToolset(curl_path="curl").fetch_web(URL)


def test_fetch_web_reports_empty_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, _completed(b"<html><body><p>short</p></body></html>"))
    monkeypatch.setattr("trafilatura.extract", lambda *_args, **_kwargs: None)
    with pytest.raises(ToolError, match="未能从网页中提取正文"):
        WebToolset(curl_path="curl").fetch_web(URL)


def test_fetch_web_truncates_long_content(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, _completed(_html().encode()))
    output = WebToolset(
        limits=AgentLimits(default_max_chars=120), curl_path="curl"
    ).fetch_web(URL)
    assert "正文已截断" in output
    assert len(output) < len(_html())


def test_fetch_web_is_registered_for_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, _completed(_html().encode()))
    toolset = WebToolset(limits=AgentLimits(), curl_path="curl")
    registry = ToolRegistry(toolset.registry_handlers(), WEB_TOOL_SPECS)

    assert [spec["function"]["name"] for spec in registry.schema()] == ["fetchWeb"]
    assert "fetchWeb" in {spec.name for spec in ALL_TOOL_SPECS}
    assert len(ALL_TOOL_SPECS) == (
        len(FILE_TOOL_SPECS)
        + len(WEB_TOOL_SPECS)
        + len(SEARCH_TOOL_SPECS)
        + len(RUNCLI_TOOL_SPECS)
    )

    result = registry.execute("fetchWeb", f'{{"url": "{URL}"}}')
    assert result.ok
    assert URL in result.content


def test_fetch_web_registry_reports_invalid_arguments() -> None:
    registry = ToolRegistry(
        WebToolset(curl_path="curl").registry_handlers(), WEB_TOOL_SPECS
    )
    result = registry.execute("fetchWeb", "{}")
    assert not result.ok
    assert "参数不匹配" in result.content
