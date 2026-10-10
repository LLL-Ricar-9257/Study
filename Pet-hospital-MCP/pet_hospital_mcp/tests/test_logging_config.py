"""结构化日志与递归脱敏。"""

from __future__ import annotations

import json
import logging
import re

import httpx
import pytest

from pet_hospital_mcp.logging_config import (
    REDACTED,
    JsonFormatter,
    configure_logging,
    get_logger,
    redact,
    tool_call_scope,
)


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    return caplog


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key",
    ["ownerPhone", "owner_phone", "OwnerPhone", "OWNER_PHONE", "owner-phone", "ownerAddr", "owner_addr", "chipNo", "chip_no"],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    assert redact({key: "13800001111"}) == {key: REDACTED}


def test_redaction_is_recursive() -> None:
    payload = {
        "q": "旺财",
        "owner": {"ownerPhone": "13800001111", "ownerAddr": "某地", "name": "张三"},
        "items": [
            {"id": "PET-1", "chipNo": "CHIP-1", "ownerPhone": "13900002222"},
            [{"chip_no": "CHIP-2"}],
        ],
    }
    result = redact(payload)

    assert result["q"] == "旺财"
    assert result["owner"] == {"ownerPhone": REDACTED, "ownerAddr": REDACTED, "name": "张三"}
    assert result["items"][0] == {"id": "PET-1", "chipNo": REDACTED, "ownerPhone": REDACTED}
    assert result["items"][1][0] == {"chip_no": REDACTED}


def test_redaction_keeps_non_sensitive_values() -> None:
    payload = {"species": "犬", "total": 12, "page": 2, "ok": True, "none": None}
    assert redact(payload) == payload


# ---------------------------------------------------------------------------
# 工具调用日志
# ---------------------------------------------------------------------------
def test_tool_call_scope_logs_required_fields(captured: pytest.LogCaptureFixture) -> None:
    logger = get_logger("test.tool_call")

    with tool_call_scope(logger, "list_pets", {"species": "犬", "ownerPhone": "13800001111"}):
        pass

    record = captured.records[-1]
    payload = _render(record)
    for field in ("timestamp", "tool_name", "params", "status", "duration_ms"):
        assert field in payload
    assert payload["tool_name"] == "list_pets"
    assert payload["status"] == "success"
    assert isinstance(payload["duration_ms"], float)
    assert payload["params"]["species"] == "犬"
    assert payload["params"]["ownerPhone"] == REDACTED


def test_tool_call_scope_records_failure(captured: pytest.LogCaptureFixture) -> None:
    logger = get_logger("test.tool_call")

    with tool_call_scope(logger, "list_pets", {}) as record:
        record.fail("BACKEND_TIMEOUT")

    payload = _render(captured.records[-1])
    assert payload["status"] == "error"
    assert payload["error_code"] == "BACKEND_TIMEOUT"


def test_tool_call_scope_logs_on_failure(captured: pytest.LogCaptureFixture) -> None:
    logger = get_logger("test.tool_call")

    with pytest.raises(RuntimeError):
        with tool_call_scope(logger, "list_pets", {}):
            raise RuntimeError("boom")

    payload = _render(captured.records[-1])
    assert payload["status"] == "crash"


def _render(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


# ---------------------------------------------------------------------------
# 端到端：工具调用产生的日志确实脱敏
# ---------------------------------------------------------------------------
async def test_tool_call_does_not_log_raw_phone(
    mcp_server, captured: pytest.LogCaptureFixture
) -> None:
    await mcp_server.call_tool("list_pets", {"ownerPhone": "13800001111"})

    rendered = _render_own_logs(captured.records)
    assert "13800001111" not in rendered
    assert f'"ownerPhone": "{REDACTED}"' in rendered


async def test_error_log_does_not_include_upstream_body(
    make_client, captured: pytest.LogCaptureFixture
) -> None:
    from pet_hospital_mcp.config import Settings
    from pet_hospital_mcp.server import create_server

    from .conftest import FakeBackend

    secret = "leaky-upstream-body"
    fake = FakeBackend(responses=[httpx.Response(500, text=secret)])
    client = make_client(fake)
    server = create_server(Settings(retry_backoff=0.0), client=client)
    try:
        with pytest.raises(Exception):
            await server.call_tool("list_pets", {})
    finally:
        await client.aclose()

    assert secret not in _render_own_logs(captured.records)


def _render_own_logs(records: list[logging.LogRecord]) -> str:
    """只渲染本服务自己的日志；httpx 属于第三方，其 INFO 行为不在脱敏范围内。"""
    formatter = JsonFormatter()
    return "\n".join(
        formatter.format(record)
        for record in records
        if record.name.startswith("pet_hospital_mcp")
    )


# ---------------------------------------------------------------------------
# Formatter 与配置
# ---------------------------------------------------------------------------
def test_formatter_emits_single_line_json() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    line = JsonFormatter().format(record)
    assert "\n" not in line
    assert json.loads(line)["message"] == "hello"


def test_configure_logging_is_idempotent() -> None:
    configure_logging("INFO")
    configure_logging("DEBUG")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    # httpx 的 INFO 日志会把工具日志淹没，必须压下去。
    assert logging.getLogger("httpx").level == logging.WARNING


def test_timestamp_is_iso_like() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}", _render(record)["timestamp"])