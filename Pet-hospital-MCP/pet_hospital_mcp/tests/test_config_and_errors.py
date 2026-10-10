"""配置解析与错误模型。"""

from __future__ import annotations

import pytest

from pet_hospital_mcp.config import (
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PORT,
    DEFAULT_PET_HOSPITAL_BASE_URL,
    Settings,
)
from pet_hospital_mcp.errors import (
    ErrorCode,
    InternalError,
    PetHospitalMCPError,
    ToolErrorOutput,
    ValidationError,
)


def test_defaults() -> None:
    settings = Settings.from_env({})
    assert settings.pet_hospital_base_url == DEFAULT_PET_HOSPITAL_BASE_URL
    assert settings.host == DEFAULT_MCP_HOST
    assert settings.port == DEFAULT_MCP_PORT
    assert settings.mcp_path == "/mcp"


def test_env_overrides() -> None:
    settings = Settings.from_env(
        {
            "PET_HOSPITAL_BASE_URL": "http://192.168.1.10:9090/",
            "MCP_HOST": "0.0.0.0",
            "MCP_PORT": "9000",
            "MCP_PATH": "rpc",
            "MCP_REQUEST_TIMEOUT": "3.5",
            "MCP_MAX_RETRIES": "5",
            "MCP_RETRY_BACKOFF": "0.5",
            "MCP_LOG_LEVEL": "debug",
        }
    )
    assert settings.pet_hospital_base_url == "http://192.168.1.10:9090"
    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.mcp_path == "/rpc"
    assert settings.request_timeout == 3.5
    assert settings.max_retries == 5
    assert settings.retry_backoff == 0.5
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    "env",
    [
        {"MCP_PORT": "not-a-number"},
        {"MCP_PORT": "-1"},
        {"MCP_REQUEST_TIMEOUT": "abc"},
        {"MCP_REQUEST_TIMEOUT": "-5"},
        {"MCP_MAX_RETRIES": "-2"},
        {"MCP_RETRY_BACKOFF": "-1"},
    ],
)
def test_invalid_env_falls_back(env: dict[str, str]) -> None:
    settings = Settings.from_env(env)
    assert settings == Settings.from_env({})


def test_blank_base_url_falls_back() -> None:
    assert Settings.from_env({"PET_HOSPITAL_BASE_URL": "  "}).pet_hospital_base_url == (
        DEFAULT_PET_HOSPITAL_BASE_URL
    )


# ---------------------------------------------------------------------------
# 错误模型
# ---------------------------------------------------------------------------
def test_error_serializes_to_unified_envelope() -> None:
    error = ValidationError("入参不对", details={"errors": [{"field": "page"}]})
    payload = error.to_output().model_dump()

    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message", "details"}
    assert payload["error"]["code"] == ErrorCode.VALIDATION_ERROR
    assert payload["error"]["message"] == "入参不对"


def test_error_json_is_parseable() -> None:
    import json

    assert json.loads(InternalError().to_json())["error"]["code"] == ErrorCode.INTERNAL_ERROR


def test_details_are_capped() -> None:
    error = PetHospitalMCPError(details={"many": list(range(100)), "text": "x" * 1000})
    details = error.to_output().model_dump()["error"]["details"]
    assert len(details["many"]) == 20
    assert len(details["text"]) <= 513


def test_error_output_model_rejects_extra_fields() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ToolErrorOutput.model_validate({"error": {"code": "X", "message": "y"}, "extra": 1})