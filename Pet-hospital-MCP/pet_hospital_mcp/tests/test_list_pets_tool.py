"""``list_pets`` 工具层：参数转发、严格入参校验、输出模型与统一错误。"""

from __future__ import annotations

import math
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.errors import ErrorCode
from pet_hospital_mcp.rest_client import PetHospitalRestClient
from pet_hospital_mcp.server import create_server
from pet_hospital_mcp.tools.list_pets import (
    QUERY_PARAM_NAMES,
    SORT_FIELDS,
    SORT_ORDERS,
    SPECIES,
    STATUS,
    ListPetsInput,
    PetListData,
)

from .conftest import (
    SAMPLE_PET_WITH_NULLS,
    FakeBackend,
    make_envelope,
    make_list_data,
    parse_tool_error,
)

ALL_ARGS: dict[str, Any] = {
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


class ExplodingClient:
    """非预期异常的替身：抛出的不是本服务的结构化异常。"""

    async def list_pets(self, params: dict[str, str]) -> Any:
        raise RuntimeError("secret internal detail")

    async def aclose(self) -> None:
        return None


async def _call_error(server, args: dict) -> dict:
    """调用工具并把统一错误信封抠出来。"""
    with pytest.raises(ToolError) as excinfo:
        await server.call_tool("list_pets", args)
    return parse_tool_error(str(excinfo.value))


# ---------------------------------------------------------------------------
# 1. 正常调用：全部过滤 / 排序 / 分页参数正确转发
# ---------------------------------------------------------------------------
async def test_forwards_all_query_params(mcp_server, backend: FakeBackend) -> None:
    result = await mcp_server.call_tool("list_pets", ALL_ARGS)

    assert result.is_error is False
    assert backend.last.method == "GET"
    assert backend.last.path == "/api/v1/pets"
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


async def test_no_arguments_sends_no_query(mcp_server, backend: FakeBackend) -> None:
    await mcp_server.call_tool("list_pets", {})
    assert backend.last.query == {}


async def test_partial_arguments_only_forward_given_ones(
    mcp_server, backend: FakeBackend
) -> None:
    await mcp_server.call_tool("list_pets", {"species": "猫", "page": 3})
    assert {key: values[0] for key, values in backend.last.query.items()} == {
        "species": "猫",
        "page": "3",
    }


async def test_min_only_enables_cost_filter(mcp_server, backend: FakeBackend) -> None:
    """Go 端只要出现 min 或 max 就启用花费区间过滤。"""
    await mcp_server.call_tool("list_pets", {"min": 500})
    assert "max" not in backend.last.query
    assert backend.last.single("min") == "500"


def test_query_covers_exactly_the_backend_contract() -> None:
    """既不漏传后端支持的参数，也不引入适配器私有参数。"""
    assert set(QUERY_PARAM_NAMES) == {
        "q",
        "name",
        "ownerName",
        "ownerPhone",
        "species",
        "doctor",
        "disease",
        "status",
        "min",
        "max",
        "sortBy",
        "order",
        "page",
        "pageSize",
    }
    assert list(QUERY_PARAM_NAMES) == [
        field.alias or name for name, field in ListPetsInput.model_fields.items()
    ]


# ---------------------------------------------------------------------------
# 成功输出：对应 Go 响应里的 data
# ---------------------------------------------------------------------------
async def test_success_output_matches_go_data(mcp_server) -> None:
    result = await mcp_server.call_tool("list_pets", {})
    structured = result.structured_content

    assert set(structured) == {"items", "total", "page", "pageSize", "totalPages", "totalCost"}
    assert structured["total"] == 1
    assert structured["page"] == 1
    assert structured["pageSize"] == 10
    assert structured["totalPages"] == 1
    assert structured["totalCost"] == 180.0

    pet = structured["items"][0]
    assert pet["id"] == "PET-000001"
    assert pet["ownerPhone"] == "13800001111"
    assert pet["visitCount"] == 1
    assert pet["records"][0]["visitDate"] == "2025-01-02"
    assert pet["charges"][0]["amount"] == 180.0


async def test_output_schema_is_published(mcp_server) -> None:
    tool = (await mcp_server.list_tools())[0]
    assert tool.output_schema is not None
    assert set(tool.output_schema["properties"]) == {
        "items",
        "total",
        "page",
        "pageSize",
        "totalPages",
        "totalCost",
    }


async def test_null_records_and_charges_are_preserved(make_client) -> None:
    """Go 用 nil slice 序列化出 null，适配层不能把它变成 []。"""
    fake = FakeBackend(
        responses=[httpx.Response(200, json=make_envelope(make_list_data([SAMPLE_PET_WITH_NULLS])))]
    )
    client = make_client(fake)
    try:
        data = await client.list_pets({})
    finally:
        await client.aclose()

    parsed = PetListData.model_validate(data)
    assert parsed.items[0].records is None
    assert parsed.items[0].charges is None
    dumped = parsed.model_dump(by_alias=True)["items"][0]
    assert dumped["records"] is None
    assert dumped["charges"] is None


async def test_empty_items_list(make_client) -> None:
    fake = FakeBackend(responses=[httpx.Response(200, json=make_envelope(make_list_data([])))])
    client = make_client(fake)
    server = create_server(Settings(retry_backoff=0.0), client=client)
    try:
        result = await server.call_tool("list_pets", {})
    finally:
        await client.aclose()

    assert result.structured_content["items"] == []
    assert result.structured_content["total"] == 0


# ---------------------------------------------------------------------------
# 2. 输入参数校验失败
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("species", [*SPECIES, None])
async def test_valid_species_accepted(mcp_server, species) -> None:
    args = {} if species is None else {"species": species}
    result = await mcp_server.call_tool("list_pets", args)
    assert result.is_error is False


@pytest.mark.parametrize("status", [*STATUS, None])
async def test_valid_status_accepted(mcp_server, status) -> None:
    result = await mcp_server.call_tool("list_pets", {} if status is None else {"status": status})
    assert result.is_error is False


@pytest.mark.parametrize("sort_by", [*SORT_FIELDS, None])
async def test_valid_sort_field_accepted(mcp_server, sort_by) -> None:
    args = {} if sort_by is None else {"sortBy": sort_by}
    result = await mcp_server.call_tool("list_pets", args)
    assert result.is_error is False


@pytest.mark.parametrize("order", [*SORT_ORDERS, None])
async def test_valid_order_accepted(mcp_server, order) -> None:
    result = await mcp_server.call_tool("list_pets", {} if order is None else {"order": order})
    assert result.is_error is False


@pytest.mark.parametrize("page", [1, 2, 10_000])
async def test_valid_page_accepted(mcp_server, page) -> None:
    assert (await mcp_server.call_tool("list_pets", {"page": page})).is_error is False


@pytest.mark.parametrize("page_size", [1, 20, 500])
async def test_valid_page_size_accepted(mcp_server, page_size) -> None:
    result = await mcp_server.call_tool("list_pets", {"pageSize": page_size})
    assert result.is_error is False


@pytest.mark.parametrize(
    ("args", "field", "reason"),
    [
        ({"species": "鼠"}, "species", "literal_error"),
        ({"species": "dog"}, "species", "literal_error"),
        ({"species": 42}, "species", "literal_error"),
        ({"status": "已出院"}, "status", "literal_error"),
        ({"sortBy": "ownerPhone"}, "sortBy", "literal_error"),
        ({"order": "DESC"}, "order", "literal_error"),
        ({"page": 0}, "page", "greater_than_equal"),
        ({"page": -1}, "page", "greater_than_equal"),
        ({"pageSize": 0}, "pageSize", "greater_than_equal"),
        ({"pageSize": 501}, "pageSize", "less_than_equal"),
        ({"min": -1}, "min", "greater_than_equal"),
        ({"max": -0.5}, "max", "greater_than_equal"),
        ({"min": 10, "max": 5}, "(cross-field)", "value_error"),
        ({"nope": 1}, "nope", "extra_forbidden"),
        ({"ownerPhone": "13800001111", "unexpected": "x"}, "unexpected", "extra_forbidden"),
        ({"page": "abc"}, "page", "int_parsing"),
        ({"min": math.inf}, "min", "finite_number"),
        ({"max": math.inf}, "max", "finite_number"),
        ({"max": -math.inf}, "max", "greater_than_equal"),
        ({"page": True}, "page", "value_error"),
        ({"pageSize": False}, "pageSize", "value_error"),
    ],
)
async def test_invalid_arguments_return_unified_error(mcp_server, args, field, reason) -> None:
    payload = await _call_error(mcp_server, args)

    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message", "details"}
    assert payload["error"]["code"] == ErrorCode.VALIDATION_ERROR
    assert payload["error"]["message"]
    assert {"field": field, "reason": reason} in payload["error"]["details"]["errors"]


async def test_nan_is_rejected(mcp_server) -> None:
    payload = await _call_error(mcp_server, {"min": math.nan})
    assert payload["error"]["code"] == ErrorCode.VALIDATION_ERROR


async def test_validation_error_never_leaks_pydantic_internals(mcp_server) -> None:
    with pytest.raises(ToolError) as excinfo:
        await mcp_server.call_tool("list_pets", {"page": "abc"})
    text = str(excinfo.value)

    for leak in ("errors.pydantic.dev", "ValidationError", "Traceback", "input_value", "line "):
        assert leak not in text


async def test_validation_error_does_not_call_backend(mcp_server, backend: FakeBackend) -> None:
    await _call_error(mcp_server, {"species": "鼠"})
    assert backend.call_count == 0


# ---------------------------------------------------------------------------
# 3./4./5. 上游异常统一归类
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(500, json={"message": "boom"}), ErrorCode.BACKEND_API_ERROR),
        (httpx.Response(404, json={"message": "not found"}), ErrorCode.BACKEND_API_ERROR),
        (httpx.Response(400, json={"message": "参数错误"}), ErrorCode.BACKEND_API_ERROR),
        (httpx.Response(200, text="not json"), ErrorCode.BACKEND_INVALID_RESPONSE),
        (httpx.Response(200, json=[1, 2, 3]), ErrorCode.BACKEND_INVALID_RESPONSE),
        (
            httpx.Response(200, json={"code": 200, "message": "ok"}),
            ErrorCode.BACKEND_INVALID_RESPONSE,
        ),
        (
            httpx.Response(200, json=make_envelope({"items": "not-a-list"})),
            ErrorCode.BACKEND_INVALID_RESPONSE,
        ),
        (
            httpx.Response(200, json=make_envelope({"items": [{"id": "P1"}]})),
            ErrorCode.BACKEND_INVALID_RESPONSE,
        ),
        (
            httpx.Response(200, json=make_envelope({"items": [], "total": "many"})),
            ErrorCode.BACKEND_INVALID_RESPONSE,
        ),
        (httpx.ReadTimeout("slow"), ErrorCode.BACKEND_TIMEOUT),
        (httpx.ConnectError("refused"), ErrorCode.BACKEND_UNAVAILABLE),
        (httpx.ReadError("reset"), ErrorCode.BACKEND_UNAVAILABLE),
    ],
)
async def test_upstream_failures_map_to_error_codes(make_client, response, code: str) -> None:
    fake = FakeBackend(responses=[response])
    client = make_client(fake)
    server = create_server(Settings(retry_backoff=0.0), client=client)
    try:
        payload = await _call_error(server, {})
    finally:
        await client.aclose()

    assert payload["error"]["code"] == code


async def test_unexpected_exception_becomes_internal_error() -> None:
    server = create_server(Settings(retry_backoff=0.0), client=ExplodingClient())
    payload = await _call_error(server, {})

    assert payload["error"]["code"] == ErrorCode.INTERNAL_ERROR
    assert "secret internal detail" not in str(payload)


async def test_details_never_contain_upstream_body(make_client) -> None:
    secret = "internal-detail-should-not-leak"
    fake = FakeBackend(responses=[httpx.Response(500, text=secret)])
    client = make_client(fake)
    server = create_server(Settings(retry_backoff=0.0), client=client)
    try:
        payload = await _call_error(server, {})
    finally:
        await client.aclose()

    assert secret not in str(payload)
    assert payload["error"]["details"]["status"] == 500


# ---------------------------------------------------------------------------
# 输入模型本身的单元测试（不经 MCP 层）
# ---------------------------------------------------------------------------
def test_input_model_rejects_unknown_field() -> None:
    with pytest.raises(Exception):
        ListPetsInput(unknown="x")


def test_input_model_formats_numbers() -> None:
    assert ListPetsInput(min=1000.0, max=2500.5, page=1, pageSize=20).to_query() == {
        "min": "1000",
        "max": "2500.5",
        "page": "1",
        "pageSize": "20",
    }


def test_input_model_omits_absent_values() -> None:
    assert ListPetsInput().to_query() == {}


def test_input_model_accepts_alias_and_name() -> None:
    by_alias = ListPetsInput(ownerName="张三", sortBy="name", pageSize=5)
    by_name = ListPetsInput(owner_name="张三", sort_by="name", page_size=5)
    assert by_alias.to_query() == by_name.to_query()


def test_client_base_url_is_configurable(settings: Settings) -> None:
    client = PetHospitalRestClient(settings)
    assert client.base_url.rstrip("/") == "http://127.0.0.1:8080"