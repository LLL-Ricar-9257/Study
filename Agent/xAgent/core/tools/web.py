"""网页抓取工具：curl 下载 + trafilatura 正文提取。

设计约束：
- 只允许 http/https URL；
- 通过参数列表调用 curl，不使用 shell，避免命令注入；
- 限制下载体积、连接时间和总耗时；
- 所有可预期失败统一转换为 ``ToolError``，由注册表回传给模型。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from urllib.parse import urlsplit

from ..config import AgentLimits
from ..errors import ToolError

_DEFAULT_TIMEOUT_SECONDS = 20.0
_MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
_META_MARKER = b"\nXAGENT-CURL-META\t"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HTML_MIME_TYPES = {"text/html", "application/xhtml+xml"}

_CURL_ERRORS = {
    6: "无法解析网页域名",
    7: "无法连接到网页服务器",
    22: "网页返回 HTTP 错误",
    28: "访问网页超时",
    35: "SSL/TLS 握手失败",
    47: "重定向次数过多",
    52: "服务器返回了空响应",
    56: "接收网页数据失败",
    60: "HTTPS 证书校验失败",
    63: "网页内容超过大小限制",
}


def _validate_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        raise ToolError("网页 URL 不能为空")
    if any(char.isspace() for char in value):
        raise ToolError(f"网页 URL 不合法：{value}")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ToolError(f"网页 URL 不合法：{value}") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ToolError(f"只支持 http/https 网页 URL：{value}")
    return value


def _split_response(raw: bytes) -> tuple[bytes, str, str]:
    """拆分 curl 的响应正文和 ``--write-out`` 追加的元信息。"""
    index = raw.rfind(_META_MARKER)
    if index < 0:
        return raw, "", ""
    body = raw[:index]
    meta = raw[index + len(_META_MARKER) :].decode("utf-8", errors="replace").strip()
    status, _, content_type = meta.partition("\t")
    return body, status.strip(), content_type.strip()


def _shorten(text: str, limit: int = 300) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


class WebToolset:
    """绑定运行限额的网页抓取工具。"""

    def __init__(
        self,
        limits: AgentLimits | None = None,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        curl_path: str | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self.limits = limits or AgentLimits()
        self.timeout_seconds = float(timeout_seconds)
        self._curl_path = curl_path

    # -- 内部辅助 ---------------------------------------------------------

    def _find_curl(self) -> str:
        curl = self._curl_path or shutil.which("curl")
        if not curl:
            raise ToolError("找不到 curl 程序，请先安装 curl 并确保它在 PATH 中")
        return curl

    def _download(self, url: str) -> tuple[bytes, str]:
        curl = self._find_curl()
        timeout = self.timeout_seconds
        command = [
            curl,
            "--location",
            "--silent",
            "--show-error",
            "--fail",
            "--compressed",
            "--proto",
            "=http,https",
            "--proto-redir",
            "=http,https",
            "--max-time",
            f"{timeout:g}",
            "--connect-timeout",
            f"{min(timeout, 10.0):g}",
            "--max-filesize",
            str(_MAX_DOWNLOAD_BYTES),
            "--user-agent",
            _USER_AGENT,
            "--header",
            "Accept: text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "--header",
            "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
            "--write-out",
            "\nXAGENT-CURL-META\t%{http_code}\t%{content_type}",
            "--",
            url,
        ]

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout + 5.0,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"访问网页超时（超过 {timeout:g} 秒）：{url}") from exc
        except FileNotFoundError as exc:
            raise ToolError("找不到 curl 程序，请先安装 curl 并确保它在 PATH 中") from exc
        except OSError as exc:
            raise ToolError(f"启动 curl 失败：{exc}") from exc

        body, status, content_type = _split_response(completed.stdout)
        if completed.returncode != 0:
            reason = _CURL_ERRORS.get(
                completed.returncode, f"curl 退出码 {completed.returncode}"
            )
            if status and status != "000":
                reason += f"（HTTP {status}）"
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            message = f"访问网页失败：{url}\n原因：{reason}"
            if detail:
                message += f"\n详情：{_shorten(detail)}"
            raise ToolError(message)

        if len(body) > _MAX_DOWNLOAD_BYTES:
            raise ToolError(
                "网页内容超过大小限制"
                f"（{_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB）：{url}"
            )
        if not body.strip():
            raise ToolError(f"网页返回内容为空：{url}")
        return body, content_type

    def _extract_markdown(self, body: bytes, url: str) -> tuple[str, str]:
        try:
            from trafilatura import extract, extract_metadata, load_html
        except ImportError as exc:  # 依赖缺失时给出可执行的提示
            raise ToolError(
                "缺少 trafilatura 依赖，请运行：pip install trafilatura"
            ) from exc

        try:
            document = load_html(body)
        except Exception as exc:
            raise ToolError(f"HTML 解析失败：{url}：{_shorten(str(exc))}") from exc
        if document is None:
            raise ToolError(f"HTML 解析失败：无法解析网页内容：{url}")

        try:
            markdown = extract(
                document,
                url=url,
                output_format="markdown",
                include_formatting=True,
                include_links=True,
                include_tables=True,
                include_comments=False,
                deduplicate=True,
            )
            metadata = extract_metadata(document, default_url=url)
        except Exception as exc:
            raise ToolError(f"网页正文提取失败：{url}：{_shorten(str(exc))}") from exc

        text = (markdown or "").strip()
        if not text:
            raise ToolError(
                "未能从网页中提取正文，可能是动态渲染页面、需要登录或正文过短："
                f"{url}"
            )
        title = ""
        if metadata is not None and metadata.title:
            title = " ".join(metadata.title.split())
        return text, title

    # -- 工具实现 ---------------------------------------------------------

    def fetch_web(self, url: str) -> str:
        """获取指定网页的主要内容，并转换为 Markdown 返回。"""
        target = _validate_url(url)
        body, content_type = self._download(target)

        if content_type:
            mime = content_type.split(";", 1)[0].strip().lower()
            if mime and mime not in _HTML_MIME_TYPES:
                raise ToolError(
                    f"网页返回的内容类型不是 HTML（{mime}），无法提取正文：{target}"
                )

        markdown, title = self._extract_markdown(body, target)

        limit = max(1, int(self.limits.default_max_chars))
        truncated = len(markdown) > limit
        content = markdown[:limit]

        lines = [f"网页 URL：{target}"]
        if title:
            lines.append(f"标题：{title}")
        lines.append(f"正文（Markdown，共 {len(markdown)} 字符）：")
        lines.append("")
        lines.append(content)
        if truncated:
            lines.append("")
            lines.append(
                f"（正文已截断，仅显示前 {limit} 个字符，共 {len(markdown)} 个字符。）"
            )
        return "\n".join(lines)

    def registry_handlers(self) -> dict[str, Callable[..., str]]:
        """返回工具名到绑定方法的映射，供 ToolRegistry 使用。"""
        return {"fetchWeb": self.fetch_web}
