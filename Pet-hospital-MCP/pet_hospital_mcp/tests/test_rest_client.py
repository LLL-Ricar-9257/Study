"""REST 客户端：请求转发、超时/重试、上游错误与非法响应归类。"""

from __future__ import annotations

import httpx
import pytest

from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.errors import (
    BackendApiError,
    BackendInvalidResponseError,
    BackendTimeoutError,
    BackendUnavailableError,
    ErrorCode,
)
from pet_hospital_mcp.rest_client import PETS_PATH, PetHospitalRestClient

from .conftest import FakeBackend, make_envelope, make_list_data

# ---------------------------------------------------------------------------
# 正常调用：路径与查询参数
# ---------------------------------------------------------------------------
async def test_forwards_path_and_every_query_param(
    rest_client: PetHospitalRestClient, backend: FakeBackend
) -> None:
    await rest_client.list_pets(
        {
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
    )

    request = backend.last
    assert request.method == "GET"
    assert request.path == PETS_PATH
    assert request.query == {
        "q": ["肠胃炎"],
        "name": ["旺财"],
        "ownerName": ["张三"],
        "ownerPhone": ["13800001111"],
        "species": ["犬"],
        "doctor": ["李医生"],
        "disease": ["急性肠胃炎"],
        "status": ["待就诊"],
        "min": ["100"],
        "max": ["5000"],
        "sortBy": ["totalCost"],
        "order": ["desc"],
        "page": ["2"],
        "pageSize": ["50"],
    }


async def test_unwraps_envelope_data(rest_client: PetHospitalRestClient) -> None:
    data = await rest_client.list_pets({})
    assert data == make_list_data()


async def test_no_params_sends_bare_path(
    rest_client: PetHospitalRestClient, backend: FakeBackend
) -> None:
    await rest_client.list_pets({})
    assert backend.last.query == {}


# ---------------------------------------------------------------------------
# 4xx / 5xx
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 499])
async def test_client_error_status_is_backend_api_error(
    make_client, status: int
) -> None:
    fake = FakeBackend(
        responses=[httpx.Response(status, json=make_envelope(None, code=status, message="参数不合法"))]
    )
    client = make_client(fake)
    try:
        with pytest.raises(BackendApiError) as excinfo:
            await client.list_pets({"species": "犬"})
    finally:
        await client.aclose()

    assert excinfo.value.code == ErrorCode.BACKEND_API_ERROR
    assert excinfo.value.details["status"] == status
    assert excinfo.value.details["upstream_message"] == "参数不合法"
    # 4xx 不重试
    assert fake.call_count == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_error_status_retries_then_fails(make_client, status: int) -> None:
    fake = FakeBackend(responses=[httpx.Response(status, json={"message": "boom"})])
    client = make_client(fake)
    try:
        with pytest.raises(BackendApiError) as excinfo:
            await client.list_pets({})
    finally:
        await client.aclose()

    assert excinfo.value.code == ErrorCode.BACKEND_API_ERROR
    assert fake.call_count == 3  # 首次 + 2 次重试
    assert excinfo.value.details["attempts"] == 3


async def test_500_then_success_is_transparent(make_client) -> None:
    fake = FakeBackend(
        responses=[
            httpx.Response(503, json={"message": "restarting"}),
            httpx.Response(200, json=make_envelope(make_list_data())),
        ]
    )
    client = make_client(fake)
    try:
        data = await client.list_pets({})
    finally:
        await client.aclose()

    assert data["total"] == 1
    assert fake.call_count == 2


async def test_non_200_business_code_is_backend_api_error(make_client) -> None:
    """HTTP 200 但信封里的 code 不是 200，同样算上游错误。"""
    fake = FakeBackend(
        responses=[httpx.Response(200, json=make_envelope(None, code=500, message="内部错误"))]
    )
    client = make_client(fake)
    try:
        with pytest.raises(BackendApiError) as excinfo:
            await client.list_pets({})
    finally:
        await client.aclose()

    assert excinfo.value.details["upstream_code"] == 500


# ---------------------------------------------------------------------------
# 超时与连接异常
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow")])
async def test_timeout_is_backend_timeout(make_client, exc: Exception) -> None:
    fake = FakeBackend(responses=[exc, exc, exc])
    client = make_client(fake)
    try:
        with pytest.raises(BackendTimeoutError) as excinfo:
            await client.list_pets({})
    finally:
        await client.aclose()

    assert excinfo.value.code == ErrorCode.BACKEND_TIMEOUT
    assert excinfo.value.details["attempts"] == 3
    assert fake.call_count == 3


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadError("reset"),
        httpx.RemoteProtocolError("bad"),
    ],
)
async def test_transport_error_is_backend_unavailable(make_client, exc: Exception) -> None:
    fake = FakeBackend(responses=[exc, exc, exc])
    client = make_client(fake)
    try:
        with pytest.raises(BackendUnavailableError) as excinfo:
            await client.list_pets({})
    finally:
        await client.aclose()

    assert excinfo.value.code == ErrorCode.BACKEND_UNAVAILABLE
    assert excinfo.value.details["reason"] == type(exc).__name__


async def test_no_retry_when_disabled(make_client) -> None:
    fake = FakeBackend(responses=[httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")])
    client = PetHospitalRestClient(
        Settings(max_retries=0, retry_backoff=0.0),
        transport=httpx.MockTransport(fake.handler),
    )
    try:
        with pytest.raises(BackendTimeoutError):
            await client.list_pets({})
    finally:
        await client.aclose()

    assert fake.call_count == 1


# ---------------------------------------------------------------------------
# 非法响应
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, content=b"\xff\xfe\x00broken"),
        httpx.Response(200, json=[1, 2, 3]),
        httpx.Response(200, json={"code": 200, "message": "ok"}),
    ],
)
async def test_malformed_response_is_backend_invalid_response(make_client, response) -> None:
    fake = FakeBackend(responses=[response])
    client = make_client(fake)
    try:
        with pytest.raises(BackendInvalidResponseError) as excinfo:
            await client.list_pets({})
    finally:
        await client.aclose()

    assert excinfo.value.code == ErrorCode.BACKEND_INVALID_RESPONSE


async def test_error_body_without_json_does_not_leak_body(make_client) -> None:
    """错误响应体不可解析时，details 里绝不能出现原文。"""
    secret = "STACKTRACE-AT-/secret/path"
    fake = FakeBackend(responses=[httpx.Response(500, text=secret)])
    client = make_client(fake)
    try:
        with pytest.raises(BackendApiError) as excinfo:
            await client.list_pets({})
    finally:
        await client.aclose()

    rendered = repr(excinfo.value.to_output().model_dump())
    assert secret not in rendered


async def test_context_manager_closes_client(settings: Settings) -> None:
    fake = FakeBackend()
    async with PetHospitalRestClient(settings, transport=httpx.MockTransport(fake.handler)) as client:
        await client.list_pets({})
    assert client._client.is_closed