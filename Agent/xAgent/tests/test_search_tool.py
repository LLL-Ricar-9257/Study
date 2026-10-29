from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

from core.config import AgentLimits, SearchSettings
from core.errors import ToolError
from core.tools.registry import ToolRegistry
from core.tools.search import SearchToolset
from core.tools.specs import ALL_TOOL_SPECS, SEARCH_TOOL_SPECS

ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"


class FakeResponse:
    def __init__(self, data: bytes, status: int = 200) -> None:
        self._data = data
        self.status = status

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            return self._data
        return self._data[:amount]

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def _response_body() -> bytes:
    return json.dumps(
        {
            "references": [
                {
                    "id": 1,
                    "title": "北京天气预报",
                    "url": "https://www.weather.com.cn/beijing.shtml",
                    "content": "北京今天晴，最高气温 26 摄氏度。",
                    "date": "2026-10-26 08:00:00",
                    "type": "web",
                },
                {
                    "id": 2,
                    "title": "北京出行提示",
                    "url": "https://example.com/travel",
                    "content": "周末部分路段交通管制，建议乘坐公共交通。",
                    "date": "2026-10-25 18:00:00",
                    "type": "web",
                },
            ],
            "request_id": "req-1",
        },
        ensure_ascii=False,
    ).encode()


def _toolset(
    opener: Any,
    *,
    api_key: str = "test-key",
    top_k: int = 10,
    max_chars: int = 50_000,
    timeout_seconds: float = 20.0,
) -> SearchToolset:
    return SearchToolset(
        SearchSettings(
            api_key=api_key,
            endpoint=ENDPOINT,
            top_k=top_k,
            timeout_seconds=timeout_seconds,
        ),
        limits=AgentLimits(default_max_chars=max_chars),
        opener=opener,
    )


def test_search_returns_summary_titles_and_urls() -> None:
    calls: list[tuple[Any, dict[str, Any]]] = []

    def fake_opener(request: Any, **kwargs: Any) -> FakeResponse:
        calls.append((request, kwargs))
        return FakeResponse(_response_body())

    output = _toolset(fake_opener, top_k=2).search_web("北京 天气")

    assert "搜索关键词：北京 天气" in output
    assert "共找到 2 条参考结果" in output
    assert "北京天气预报" in output
    assert "https://www.weather.com.cn/beijing.shtml" in output
    assert "北京今天晴" in output
    assert "发布时间：2026-10-26 08:00:00" in output
    assert "北京出行提示" in output

    request, kwargs = calls[0]
    assert request.full_url == ENDPOINT
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer test-key"
    assert kwargs["timeout"] == 20.0
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["messages"] == [{"role": "user", "content": "北京 天气"}]
    assert payload["search_source"] == "baidu_search_v2"
    assert payload["resource_type_filter"] == [{"type": "web", "top_k": 2}]
    assert "test-key" not in output


def test_search_without_api_key_reports_config_hint() -> None:
    with pytest.raises(ToolError) as excinfo:
        _toolset(lambda *args, **kwargs: None, api_key="").search_web("新闻")
    message = str(excinfo.value)
    assert "baidu_qianfan_api_key" in message
    assert "XAGENT_BAIDU_QIANFAN_API_KEY" in message


def test_search_rejects_empty_query() -> None:
    with pytest.raises(ToolError, match="不能为空"):
        _toolset(lambda *args, **kwargs: None).search_web("   ")


def test_search_reports_http_error_with_api_message() -> None:
    body = json.dumps(
        {
            "code": 216003,
            "message": "Authentication error: invalid api key",
            "requestId": "req-401",
        }
    ).encode()

    def fake_opener(request: Any, **kwargs: Any) -> FakeResponse:
        raise HTTPError(
            request.full_url, 401, "Unauthorized", {}, io.BytesIO(body)
        )

    with pytest.raises(ToolError) as excinfo:
        _toolset(fake_opener).search_web("新闻")
    message = str(excinfo.value)
    assert "HTTP 401" in message
    assert "invalid api key" in message
    assert "错误码：216003" in message
    assert "req-401" in message
    assert "baidu_qianfan_api_key" in message


def test_search_reports_network_failure() -> None:
    def fake_opener(request: Any, **kwargs: Any) -> FakeResponse:
        raise URLError("getaddrinfo failed")

    with pytest.raises(ToolError, match="无法连接搜索服务"):
        _toolset(fake_opener).search_web("新闻")


def test_search_reports_timeout() -> None:
    def fake_opener(request: Any, **kwargs: Any) -> FakeResponse:
        raise TimeoutError("timed out")

    with pytest.raises(ToolError, match="网络搜索超时"):
        _toolset(fake_opener, timeout_seconds=3).search_web("新闻")


def test_search_reports_invalid_json() -> None:
    with pytest.raises(ToolError, match="无法解析搜索 API 返回的 JSON"):
        _toolset(lambda *args, **kwargs: FakeResponse(b"<html>error</html>")).search_web(
            "新闻"
        )


def test_search_reports_api_business_error() -> None:
    body = json.dumps(
        {"code": 400, "message": "参数错误", "requestId": "req-400"},
        ensure_ascii=False,
    ).encode()
    with pytest.raises(ToolError) as excinfo:
        _toolset(lambda *args, **kwargs: FakeResponse(body)).search_web("新闻")
    message = str(excinfo.value)
    assert "code=400" in message
    assert "参数错误" in message
    assert "req-400" in message


def test_search_reports_empty_results() -> None:
    body = json.dumps({"references": [], "request_id": "req-2"}).encode()
    with pytest.raises(ToolError, match="没有返回任何结果"):
        _toolset(lambda *args, **kwargs: FakeResponse(body)).search_web("新闻")


def test_search_truncates_long_output() -> None:
    body = json.dumps(
        {
            "references": [
                {
                    "title": "长文",
                    "url": "https://example.com/long",
                    "content": "很长的摘要" * 500,
                }
            ]
        },
        ensure_ascii=False,
    ).encode()
    output = _toolset(
        lambda *args, **kwargs: FakeResponse(body), max_chars=200
    ).search_web("长文")
    assert "搜索结果已截断" in output
    assert len(output) < 400


def test_search_is_registered_for_llm() -> None:
    registry = ToolRegistry(
        _toolset(lambda *args, **kwargs: FakeResponse(_response_body())).registry_handlers(),
        SEARCH_TOOL_SPECS,
    )

    assert [spec["function"]["name"] for spec in registry.schema()] == ["searchweb"]
    assert "searchweb" in {spec.name for spec in ALL_TOOL_SPECS}
    assert len(SEARCH_TOOL_SPECS) == 1

    result = registry.execute("searchweb", '{"query": "北京 天气"}')
    assert result.ok
    assert "北京天气预报" in result.content


def test_search_registry_reports_invalid_arguments() -> None:
    registry = ToolRegistry(
        _toolset(lambda *args, **kwargs: None).registry_handlers(),
        SEARCH_TOOL_SPECS,
    )
    result = registry.execute("searchweb", "{}")
    assert not result.ok
    assert "参数不匹配" in result.content
