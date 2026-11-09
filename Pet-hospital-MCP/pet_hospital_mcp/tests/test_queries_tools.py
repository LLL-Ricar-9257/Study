"""``get_pet`` / ``get_pet_summary`` / ``get_hospital_stats`` 工具层测试。

覆盖点与 ``list_pets`` 保持一致：路径与查询参数转发、入参严格校验（不合法时
**不调用后端**）、输出模型字段映射、自由文本截断、统一错误信封。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pet_hospital_mcp.errors import ErrorCode
from pet_hospital_mcp.server import create_server
from pet_hospital_mcp.tools.queries import (
    GET_HOSPITAL_STATS_TOOL,
    GET_PET_SUMMARY_TOOL,
    GET_PET_TOOL,
    MAX_SUMMARY_HISTORY,
    HospitalStatsData,
    PetSummaryData,
)

from .conftest import SAMPLE_PET, FakeBackend, make_envelope, parse_tool_error

SAMPLE_SUMMARY: dict[str, Any] = {
    "id": "PET-000001",
    "name": "旺财",
    "species": "犬",
    "ownerName": "张三",
    "ownerPhone": "13800001111",
    "doctor": "李医生",
    "disease": "急性肠胃炎",
    "status": "待就诊",
    "totalCost": 180.0,
    "visitCount": 1,
    "chargeCount": 1,
    "costByCategory": {"检查": 180.0},
    "costByDoctor": {"李医生": 180.0},
    "maxSingleCharge": 180.0,
    "avgCostPerVisit": 180.0,
    "firstVisit": "2025-01-02",
    "lastVisit": "2025-01-02",
    "historyText": "2025-01-02 李医生 急性肠胃炎",
}

SAMPLE_STATS: dict[str, Any] = {
    "totalPets": 2,
    "totalRecords": 1,
    "totalCharges": 1,
    "totalRevenue": 180.0,
    "averageCost": 90.0,
    "maxCost": 180.0,
    "bySpecies": {"犬": 1, "猫": 1},
    "byStatus": {"待就诊": 2},
    "byDoctor": {"李医生": 2},
    "revenueByDoctor": {"李医生": 180.0},
    "topSpenders": [SAMPLE_PET],
    "logGarbagePct": 0.0,
}


async def _call_error(server, name: str, args: dict[str, Any]) -> dict[str, Any]:
    with pytest.raises(ToolError) as excinfo:
        await server.call_tool(name, args)
    return parse_tool_error(str(excinfo.value))


# ---------------------------------------------------------------------------
# get_pet
# ---------------------------------------------------------------------------
async def test_get_pet_hits_detail_endpoint(mcp_server, backend: FakeBackend) -> None:
    backend.responses = [httpx.Response(200, json=make_envelope(SAMPLE_PET))]

    result = await mcp_server.call_tool(GET_PET_TOOL, {"pet_id": "PET-000001"})

    assert result.is_error is False
    assert backend.last.method == "GET"
    assert backend.last.path == "/api/v1/pets/PET-000001"
    assert backend.last.query == {}

    data = result.structured_content
    assert data["id"] == "PET-000001"
    assert data["ownerName"] == "张三"
    assert data["records"][0]["visitDate"] == "2025-01-02"
    assert data["charges"][0]["amount"] == 180.0


async def test_get_pet_rejects_path_like_ids(mcp_server, backend: FakeBackend) -> None:
    """编号里出现 ``/`` 或 ``..`` 时必须在本地拒绝，绝不拼进 URL。"""
    for bad in ("../../admin", "PET-1/records", "PET 1", "x" * 65, ""):
        payload = await _call_error(mcp_server, GET_PET_TOOL, {"pet_id": bad})
        assert payload["error"]["code"] == ErrorCode.VALIDATION_ERROR

    assert backend.call_count == 0


async def test_get_pet_rejects_unknown_fields(mcp_server, backend: FakeBackend) -> None:
    payload = await _call_error(
        mcp_server, GET_PET_TOOL, {"pet_id": "PET-000001", "include": "all"}
    )
    assert payload["error"]["code"] == ErrorCode.VALIDATION_ERROR
    assert backend.call_count == 0


async def test_get_pet_maps_404_to_backend_api_error(
    mcp_server, backend: FakeBackend
) -> None:
    backend.responses = [
        httpx.Response(
            404,
            json={"code": 404, "message": "宠物不存在：PET-999999", "data": None},
        )
    ]

    payload = await _call_error(mcp_server, GET_PET_TOOL, {"pet_id": "PET-999999"})
    assert payload["error"]["code"] == ErrorCode.BACKEND_API_ERROR
    assert payload["error"]["details"]["status"] == 404


async def test_get_pet_reports_invalid_backend_payload(
    mcp_server, backend: FakeBackend
) -> None:
    """缺主键的脏数据必须变成 BACKEND_INVALID_RESPONSE，而不是裸异常。"""
    backend.responses = [httpx.Response(200, json=make_envelope({"name": "旺财"}))]

    payload = await _call_error(mcp_server, GET_PET_TOOL, {"pet_id": "PET-000001"})
    assert payload["error"]["code"] == ErrorCode.BACKEND_INVALID_RESPONSE


# ---------------------------------------------------------------------------
# get_pet_summary
# ---------------------------------------------------------------------------
async def test_get_pet_summary_hits_summary_endpoint(
    mcp_server, backend: FakeBackend
) -> None:
    backend.responses = [httpx.Response(200, json=make_envelope(SAMPLE_SUMMARY))]

    result = await mcp_server.call_tool(GET_PET_SUMMARY_TOOL, {"pet_id": "PET-000001"})

    assert result.is_error is False
    assert backend.last.path == "/api/v1/pets/PET-000001/summary"

    data = result.structured_content
    assert data["totalCost"] == 180.0
    assert data["chargeCount"] == 1
    assert data["costByCategory"] == {"检查": 180.0}
    assert data["avgCostPerVisit"] == 180.0
    assert data["firstVisit"] == "2025-01-02"


async def test_get_pet_summary_truncates_history_text(
    mcp_server, backend: FakeBackend
) -> None:
    backend.responses = [
        httpx.Response(
            200,
            json=make_envelope({**SAMPLE_SUMMARY, "historyText": "病历" * 5_000}),
        )
    ]

    result = await mcp_server.call_tool(GET_PET_SUMMARY_TOOL, {"pet_id": "PET-000001"})
    history = result.structured_content["historyText"]

    assert history.startswith("病历" * 2)
    assert len(history) <= MAX_SUMMARY_HISTORY + 60
    assert "病史已截断" in history


async def test_summary_model_ignores_extra_backend_fields() -> None:
    """后端加字段不应破坏契约（extra="ignore"）。"""
    parsed = PetSummaryData.model_validate({**SAMPLE_SUMMARY, "futureField": 1})
    assert parsed.total_cost == 180.0


# ---------------------------------------------------------------------------
# get_hospital_stats
# ---------------------------------------------------------------------------
async def test_get_hospital_stats_forwards_top(mcp_server, backend: FakeBackend) -> None:
    backend.responses = [httpx.Response(200, json=make_envelope(SAMPLE_STATS))]

    result = await mcp_server.call_tool(GET_HOSPITAL_STATS_TOOL, {"top": 3})

    assert result.is_error is False
    assert backend.last.path == "/api/v1/stats"
    assert backend.last.single("top") == "3"

    data = result.structured_content
    assert data["totalPets"] == 2
    assert data["bySpecies"] == {"犬": 1, "猫": 1}
    assert data["topSpenders"][0]["id"] == "PET-000001"


async def test_get_hospital_stats_defaults_to_five(
    mcp_server, backend: FakeBackend
) -> None:
    backend.responses = [httpx.Response(200, json=make_envelope(SAMPLE_STATS))]
    await mcp_server.call_tool(GET_HOSPITAL_STATS_TOOL, {})
    assert backend.last.single("top") == "5"


@pytest.mark.parametrize("bad", [0, 21, -1])
async def test_get_hospital_stats_rejects_out_of_range_top(
    mcp_server, backend: FakeBackend, bad: int
) -> None:
    payload = await _call_error(mcp_server, GET_HOSPITAL_STATS_TOOL, {"top": bad})
    assert payload["error"]["code"] == ErrorCode.VALIDATION_ERROR
    assert backend.call_count == 0


async def test_hospital_stats_model_accepts_unknown_fields() -> None:
    parsed = HospitalStatsData.model_validate({**SAMPLE_STATS, "generatedBy": "go"})
    assert parsed.total_revenue == 180.0


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
async def test_unexpected_exception_becomes_internal_error(settings) -> None:
    """后端抛出的非结构化异常也不能把原始文本透给客户端。"""

    class Exploding:
        async def get_stats(self, top: int) -> Any:
            raise RuntimeError("secret internal detail")

        async def aclose(self) -> None:  # pragma: no cover - lifespan 兜底
            return None

    server = create_server(settings, client=Exploding())

    with pytest.raises(ToolError) as excinfo:
        await server.call_tool(GET_HOSPITAL_STATS_TOOL, {"top": 1})

    payload = parse_tool_error(str(excinfo.value))
    assert payload["error"]["code"] == ErrorCode.INTERNAL_ERROR
    assert payload["error"]["details"]["reason"] == "RuntimeError"
    assert "secret internal detail" not in str(excinfo.value)
