"""SDK 2.x 无状态 Streamable HTTP 连接流程（协议 2026-07-28）。

覆盖任务要求的第 7 项：

* 不发送旧 ``initialize``；
* 不要求也不返回 ``Mcp-Session-Id``；
* 使用 2026-07-28 实际规定的发现 / 调用方式；
* 验证 ``/health``；
* 验证工具可通过 HTTP MCP 端点被发现和调用。
"""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette

from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.errors import ErrorCode
from pet_hospital_mcp.server import PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION, build_app

from .conftest import (
    SAMPLE_PET_WITH_NULLS,
    FakeBackend,
    PROTOCOL_META_KEY,
    make_envelope,
    make_list_data,
    mcp_client,
    parse_tool_error,
    stateless_envelope,
    stateless_headers,
)

MCP_PATH = "/mcp"
SESSION_HEADER = "mcp-session-id"


def build_test_app(settings: Settings, backend: FakeBackend) -> Starlette:
    return build_app(settings, transport=httpx.MockTransport(backend.handler))


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
async def test_health_endpoint(mcp_app: Starlette) -> None:
    async with mcp_client(mcp_app) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["server"] == SERVER_NAME
    assert body["version"] == SERVER_VERSION
    assert body["protocolVersion"] == PROTOCOL_VERSION
    assert body["backend"] == "http://127.0.0.1:8080"


async def test_health_does_not_touch_backend(
    mcp_app: Starlette, backend: FakeBackend
) -> None:
    async with mcp_client(mcp_app) as client:
        await client.get("/health")
    assert backend.call_count == 0


def test_protocol_version_is_2026_07_28() -> None:
    assert PROTOCOL_VERSION == "2026-07-28"


def test_modern_protocol_versions_are_exactly_one() -> None:
    from mcp.shared.inbound import MODERN_PROTOCOL_VERSIONS

    assert MODERN_PROTOCOL_VERSIONS == ("2026-07-28",)


# ---------------------------------------------------------------------------
# 无状态：无需 initialize
# ---------------------------------------------------------------------------
async def test_tools_list_without_initialize(mcp_app: Starlette) -> None:
    """2026-07-28 的无状态路径不要求 initialize，直接发 tools/list。"""
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope("tools/list"),
            headers=stateless_headers("tools/list"),
        )

    assert response.status_code == 200
    payload = response.json()
    assert "error" not in payload
    tools = payload["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["list_pets"]
    assert tools[0]["inputSchema"]["additionalProperties"] is False
    assert set(tools[0]["inputSchema"]["properties"]) >= {"q", "species", "page", "pageSize"}


async def test_no_session_id_header_in_response(mcp_app: Starlette) -> None:
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope("tools/list"),
            headers=stateless_headers("tools/list"),
        )

    assert SESSION_HEADER not in response.headers
    assert response.headers.get(SESSION_HEADER) is None


async def test_two_calls_are_independent_stateless_exchanges(mcp_app: Starlette) -> None:
    """第二次调用不携带任何会话凭据，依然成功。"""
    async with mcp_client(mcp_app) as client:
        for request_id in (1, 2):
            response = await client.post(
                MCP_PATH,
                json=stateless_envelope("tools/list", request_id=request_id),
                headers=stateless_headers("tools/list"),
            )
            assert response.status_code == 200
            assert response.headers.get(SESSION_HEADER) is None
            assert response.json()["id"] == request_id


async def test_client_may_send_session_id_but_server_ignores_it(mcp_app: Starlette) -> None:
    """无状态服务不会因为客户端带了一个 session id 就复用会话。"""
    headers = stateless_headers("tools/list") | {SESSION_HEADER: "whatever-the-client-sent"}
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH, json=stateless_envelope("tools/list"), headers=headers
        )

    assert response.status_code == 200
    assert response.headers.get(SESSION_HEADER) is None


async def test_deleting_session_is_not_supported(mcp_app: Starlette) -> None:
    """无状态服务没有会话，DELETE 不被当作"结束会话"。"""
    async with mcp_client(mcp_app) as client:
        response = await client.delete(MCP_PATH, headers=stateless_headers("tools/list"))
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# 调用 list_pets
# ---------------------------------------------------------------------------
async def test_call_list_pets_over_http(mcp_app: Starlette, backend: FakeBackend) -> None:
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope(
                "tools/call",
                {"name": "list_pets", "arguments": {"species": "犬", "page": 1, "pageSize": 5}},
                name="list_pets",
            ),
            headers=stateless_headers("tools/call", "list_pets"),
        )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["total"] == 1
    assert result["structuredContent"]["items"][0]["id"] == "PET-000001"
    assert result["resultType"] == "complete"
    assert response.headers.get(SESSION_HEADER) is None

    assert backend.last.path == "/api/v1/pets"
    assert backend.last.query == {"species": ["犬"], "page": ["1"], "pageSize": ["5"]}


async def test_call_forwards_every_parameter(
    mcp_app: Starlette, backend: FakeBackend
) -> None:
    arguments = {
        "q": "肠胃炎",
        "name": "旺财",
        "ownerName": "张三",
        "ownerPhone": "13800001111",
        "species": "犬",
        "doctor": "李医生",
        "disease": "急性肠胃炎",
        "status": "待就诊",
        "min": 100,
        "max": 5000,
        "sortBy": "totalCost",
        "order": "desc",
        "page": 2,
        "pageSize": 50,
    }
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope(
                "tools/call", {"name": "list_pets", "arguments": arguments}, name="list_pets"
            ),
            headers=stateless_headers("tools/call", "list_pets"),
        )

    assert response.json()["result"]["isError"] is False
    assert {key: values[0] for key, values in backend.last.query.items()} == {
        "q": "肠胃炎",
        "name": "旺财",
        "ownerName": "张三",
        "ownerPhone": "13800001111",
        "species": "犬",
        "doctor": "李医生",
        "disease": "急性肠胃炎",
        "status": "待就诊",
        "min": "100",
        "max": "5000",
        "sortBy": "totalCost",
        "order": "desc",
        "page": "2",
        "pageSize": "50",
    }


async def test_successful_call_returns_backend_data(mcp_app: Starlette) -> None:
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope(
                "tools/call", {"name": "list_pets", "arguments": {}}, name="list_pets"
            ),
            headers=stateless_headers("tools/call", "list_pets"),
        )

    structured = response.json()["result"]["structuredContent"]
    assert set(structured) == {"items", "total", "page", "pageSize", "totalPages", "totalCost"}
    assert structured["total"] == 1
    assert structured["totalCost"] == 180.0
    assert make_list_data()["pageSize"] == structured["pageSize"]
    assert make_envelope(make_list_data())["data"]["items"][0]["id"] == "PET-000001"


async def test_null_records_survive_http_round_trip(settings: Settings) -> None:
    backend = FakeBackend(
        responses=[httpx.Response(200, json=make_envelope(make_list_data([SAMPLE_PET_WITH_NULLS])))]
    )
    app = build_test_app(settings, backend)
    async with mcp_client(app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope(
                "tools/call", {"name": "list_pets", "arguments": {}}, name="list_pets"
            ),
            headers=stateless_headers("tools/call", "list_pets"),
        )

    item = response.json()["result"]["structuredContent"]["items"][0]
    assert item["records"] is None
    assert item["charges"] is None


# ---------------------------------------------------------------------------
# 失败状态标记
# ---------------------------------------------------------------------------
async def test_validation_failure_is_marked_is_error(mcp_app: Starlette) -> None:
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope(
                "tools/call",
                {"name": "list_pets", "arguments": {"species": "鼠"}},
                name="list_pets",
            ),
            headers=stateless_headers("tools/call", "list_pets"),
        )

    result = response.json()["result"]
    assert result["isError"] is True
    assert "structuredContent" not in result
    payload = parse_tool_error(result["content"][0]["text"])
    assert payload["error"]["code"] == ErrorCode.VALIDATION_ERROR


async def test_unknown_argument_is_marked_is_error(mcp_app: Starlette) -> None:
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope(
                "tools/call",
                {"name": "list_pets", "arguments": {"pageSize": 10, "unknown": 1}},
                name="list_pets",
            ),
            headers=stateless_headers("tools/call", "list_pets"),
        )

    result = response.json()["result"]
    assert result["isError"] is True
    payload = parse_tool_error(result["content"][0]["text"])
    assert payload["error"]["details"]["errors"][0]["reason"] == "extra_forbidden"


async def test_backend_failure_is_marked_is_error(settings: Settings) -> None:
    backend = FakeBackend(responses=[httpx.Response(500, json={"message": "boom"})])
    app = build_test_app(settings, backend)
    async with mcp_client(app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope(
                "tools/call", {"name": "list_pets", "arguments": {}}, name="list_pets"
            ),
            headers=stateless_headers("tools/call", "list_pets"),
        )

    result = response.json()["result"]
    assert result["isError"] is True
    payload = parse_tool_error(result["content"][0]["text"])
    assert payload["error"]["code"] == ErrorCode.BACKEND_API_ERROR


async def test_error_text_never_leaks_internals(mcp_app: Starlette) -> None:
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope(
                "tools/call", {"name": "list_pets", "arguments": {"page": 0}}, name="list_pets"
            ),
            headers=stateless_headers("tools/call", "list_pets"),
        )
    text = response.json()["result"]["content"][0]["text"]
    for leak in ("Traceback", "pydantic", "httpx", 'File "'):
        assert leak not in text


async def test_unknown_tool_is_rejected(mcp_app: Starlette) -> None:
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope(
                "tools/call", {"name": "create_pet", "arguments": {}}, name="create_pet"
            ),
            headers=stateless_headers("tools/call", "create_pet"),
        )
    payload = response.json()
    assert payload.get("result", {}).get("isError") is True or "error" in payload


# ---------------------------------------------------------------------------
# 协议守卫
# ---------------------------------------------------------------------------
async def test_missing_envelope_meta_is_rejected(mcp_app: Starlette) -> None:
    """2026-07-28 要求 params._meta 携带协议版本与客户端能力。"""
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope("tools/list", include_meta=False),
            headers=stateless_headers("tools/list"),
        )
    assert response.status_code == 400
    assert "_meta" in response.json()["error"]["message"]


async def test_header_must_match_envelope(mcp_app: Starlette) -> None:
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope("tools/list", protocol_version="2026-07-27"),
            headers=stateless_headers("tools/list"),
        )
    assert response.status_code == 400
    assert "mcp-protocol-version" in response.json()["error"]["message"]


async def test_method_header_must_match_body(mcp_app: Starlette) -> None:
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope("tools/list"),
            headers=stateless_headers("tools/call"),
        )
    assert response.status_code == 400
    assert "mcp-method" in response.json()["error"]["message"]


async def test_name_header_must_match_body(mcp_app: Starlette) -> None:
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH,
            json=stateless_envelope(
                "tools/call", {"name": "list_pets", "arguments": {}}, name="other_tool"
            ),
            headers=stateless_headers("tools/call", "other_tool"),
        )
    assert response.status_code == 400
    assert "mcp-name" in response.json()["error"]["message"]


async def test_get_on_mcp_path_is_not_allowed(mcp_app: Starlette) -> None:
    """无状态模式没有独立的 GET 流。"""
    async with mcp_client(mcp_app) as client:
        response = await client.get(MCP_PATH, headers=stateless_headers("tools/list"))
    assert response.status_code == 405


async def test_bogus_endpoints_are_404(mcp_app: Starlette) -> None:
    async with mcp_client(mcp_app) as client:
        assert (await client.post("/sse", json={})).status_code == 404
        assert (await client.get("/messages/")).status_code == 404


async def test_envelope_meta_keys_are_the_spec_keys() -> None:
    assert PROTOCOL_META_KEY == "io.modelcontextprotocol/protocolVersion"


@pytest.mark.parametrize(
    "accept", ["application/json", "application/json, text/event-stream", "*/*"]
)
async def test_acceptable_content_types(mcp_app: Starlette, accept: str) -> None:
    headers = stateless_headers("tools/list") | {"accept": accept}
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH, json=stateless_envelope("tools/list"), headers=headers
        )
    assert response.status_code == 200


async def test_unacceptable_content_type_is_406(mcp_app: Starlette) -> None:
    headers = stateless_headers("tools/list") | {"accept": "application/xml"}
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH, json=stateless_envelope("tools/list"), headers=headers
        )
    assert response.status_code == 406


async def test_missing_content_type_is_rejected(mcp_app: Starlette) -> None:
    headers = stateless_headers("tools/list")
    headers["content-type"] = "text/plain"
    async with mcp_client(mcp_app) as client:
        response = await client.post(
            MCP_PATH, content=stateless_body(), headers=headers
        )
    assert response.status_code == 400


def stateless_body() -> bytes:
    import json

    return json.dumps(stateless_envelope("tools/list")).encode()