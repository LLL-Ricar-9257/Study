"""百度千帆 AI 搜索工具：``searchweb``。

设计约束：
- API Key 只来自配置 / 环境变量，代码中不保存任何密钥；
- 使用标准库 ``urllib`` 发起 HTTPS 请求，不额外引入依赖；
- 请求失败、HTTP 错误、API 业务错误、返回格式异常都转换为
  ``ToolError``，由注册表作为工具结果回传给模型；
- 返回内容包含搜索摘要、来源标题、URL 和发布时间，并受配置的
  字符上限约束，避免把过长内容塞进模型上下文。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ..config import AgentLimits, SearchSettings
from ..errors import ToolError

_MAX_SNIPPET_CHARS = 800
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_USER_AGENT = "xAgent/0.1 (Baidu Qianfan AI Search)"
_SEARCH_SOURCE = "baidu_search_v2"


def _compact(text: Any, limit: int | None = None) -> str:
    """把任意值压成单行文本，并按需截断。"""
    value = " ".join(str(text or "").split())
    if limit is not None and len(value) > limit:
        return value[:limit] + "..."
    return value


def _error_detail(raw: bytes) -> tuple[str, str, str]:
    """从百度返回的错误 JSON 中提取 code / message / requestId。"""
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, AttributeError):
        return "", "", ""
    if not isinstance(data, dict):
        return "", "", ""

    code = data.get("code", data.get("error_code", ""))
    message = data.get("message", data.get("msg", data.get("error_msg", "")))
    request_id = data.get("requestId", data.get("request_id", ""))
    return _compact(code), _compact(message), _compact(request_id)


def _http_error_message(status: int, raw: bytes) -> str:
    code, message, request_id = _error_detail(raw)
    reason = message or "服务端未返回错误详情"
    parts = [f"搜索 API 请求失败（HTTP {status}）：{reason}"]
    if code:
        parts.append(f"错误码：{code}")
    if request_id:
        parts.append(f"RequestId：{request_id}")
    if status in {401, 403}:
        parts.append(
            "请检查 [search] 小节中的 baidu_qianfan_api_key 是否正确、"
            "是否有权使用该 API。"
        )
    return "\n".join(parts)


def _validate_endpoint(endpoint: str) -> str:
    value = (endpoint or "").strip()
    if not value:
        raise ToolError(
            "搜索 API 地址为空。请在 config.ini 的 [search] 小节设置 api_url，"
            "或使用默认地址 https://qianfan.baidubce.com/v2/ai_search/web_search。"
        )
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ToolError(f"搜索 API 地址不合法：{value}") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ToolError(f"搜索 API 地址必须是 http/https URL：{value}")
    return value


class SearchToolset:
    """绑定搜索配置的 ``searchweb`` 工具。"""

    def __init__(
        self,
        settings: SearchSettings | None = None,
        *,
        limits: AgentLimits | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings or SearchSettings()
        self.limits = limits or AgentLimits()
        self._opener = opener or urlopen

    # -- 内部辅助 ---------------------------------------------------------

    def _ensure_configured(self) -> str:
        if not self.settings.api_key:
            raise ToolError(
                "缺少百度千帆搜索 API Key，暂时无法联网搜索。\n"
                "请在 config.ini 的 [search] 小节中设置：\n"
                "    baidu_qianfan_api_key = <你的 API Key>\n"
                "也可以设置环境变量 XAGENT_BAIDU_QIANFAN_API_KEY。\n"
                "API Key 可在百度智能云千帆控制台创建，配置后重启 xAgent 即可。"
            )
        return _validate_endpoint(self.settings.endpoint)

    def _build_payload(self, query: str) -> dict[str, Any]:
        return {
            "messages": [{"role": "user", "content": query}],
            "search_source": _SEARCH_SOURCE,
            "edition": "standard",
            "resource_type_filter": [{"type": "web", "top_k": self.settings.top_k}],
        }

    def _send(self, endpoint: str, query: str) -> dict[str, Any]:
        payload = json.dumps(
            self._build_payload(query), ensure_ascii=False
        ).encode("utf-8")
        request = Request(
            endpoint,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
        )

        try:
            with self._opener(request, timeout=self.settings.timeout_seconds) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            try:
                body = exc.read()
            except OSError:
                body = b""
            raise ToolError(_http_error_message(exc.code, body)) from exc
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise ToolError(
                    f"网络搜索超时（超过 {self.settings.timeout_seconds:g} 秒），"
                    "请稍后重试或检查网络连接。"
                ) from exc
            raise ToolError(
                f"网络搜索请求失败：无法连接搜索服务（{_compact(reason)}）。"
                "请检查网络、代理或防火墙设置。"
            ) from exc
        except TimeoutError as exc:
            raise ToolError(
                f"网络搜索超时（超过 {self.settings.timeout_seconds:g} 秒），"
                "请稍后重试或检查网络连接。"
            ) from exc
        except OSError as exc:
            raise ToolError(f"网络搜索请求失败：{_compact(exc)}") from exc

        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ToolError(
                "搜索 API 返回内容过大"
                f"（超过 {_MAX_RESPONSE_BYTES // (1024 * 1024)} MB），已中止处理。"
            )
        if not raw.strip():
            raise ToolError("搜索 API 返回了空响应，请稍后重试。")

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolError(
                "无法解析搜索 API 返回的 JSON，可能是服务异常或网络代理返回了错误页面。"
            ) from exc
        if not isinstance(data, dict):
            raise ToolError("搜索 API 返回格式异常：顶层结果不是 JSON 对象。")
        return data

    def _answer_text(self, data: dict[str, Any]) -> str:
        """兼容部分版本返回的模型总结（choices[0].message.content）。"""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        return _compact(message.get("content"))

    def _format(self, query: str, data: dict[str, Any]) -> str:
        references = data.get("references")
        answer = self._answer_text(data)

        if not isinstance(references, list):
            references = []
        items = [item for item in references if isinstance(item, dict) and item.get("url")]
        if not items and not answer:
            raise ToolError("搜索 API 没有返回任何结果，请尝试更换关键词后重试。")

        lines = [f"搜索关键词：{query}"]
        if items:
            lines.append(f"共找到 {len(items)} 条参考结果。")
        else:
            lines.append("搜索 API 未返回结构化参考结果，仅返回以下摘要。")

        if answer:
            lines.extend(["", "搜索摘要：", answer])

        for index, item in enumerate(items, 1):
            title = _compact(item.get("title") or item.get("web_anchor")) or "（无标题）"
            url = _compact(item.get("url"))
            snippet = _compact(
                item.get("content") or item.get("snippet") or item.get("web_anchor"),
                _MAX_SNIPPET_CHARS,
            )
            published = _compact(item.get("date"))
            lines.extend(["", f"[{index}] {title}", f"URL：{url}"])
            if published:
                lines.append(f"发布时间：{published}")
            if snippet:
                lines.append(f"摘要：{snippet}")

        lines.extend(["", "以上结果来自百度搜索，请结合来源链接核对关键信息。"])
        text = "\n".join(lines)

        limit = max(1, int(self.limits.default_max_chars))
        if len(text) > limit:
            text = (
                text[:limit].rstrip()
                + f"\n\n（搜索结果已截断，仅显示前 {limit} 个字符。）"
            )
        return text

    # -- 工具实现 ---------------------------------------------------------

    def search_web(self, query: str) -> str:
        """搜索互联网最新信息，返回搜索摘要、来源标题与 URL。"""
        value = " ".join((query or "").split())
        if not value:
            raise ToolError("搜索关键词 query 不能为空。")
        if len(value) > 500:
            raise ToolError("搜索关键词过长（最多 500 个字符），请精简后重试。")

        endpoint = self._ensure_configured()
        data = self._send(endpoint, value)

        code = data.get("code")
        if code not in (None, 0, "0"):
            message = _compact(data.get("message") or data.get("msg")) or "未知错误"
            request_id = _compact(data.get("requestId") or data.get("request_id"))
            detail = f"搜索 API 返回错误（code={code}）：{message}"
            if request_id:
                detail += f"\nRequestId：{request_id}"
            raise ToolError(detail)

        return self._format(value, data)

    def registry_handlers(self) -> dict[str, Callable[..., str]]:
        """返回工具名到绑定方法的映射，供 ToolRegistry 使用。"""
        return {"searchweb": self.search_web}
