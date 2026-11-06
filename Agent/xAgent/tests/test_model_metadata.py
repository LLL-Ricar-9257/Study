from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from core import LLMSettings, discover_model_metadata
from core.model_metadata import _FetchError


def _settings(
    base_url: str = "https://api.deepseek.com/v1",
    model: str = "m",
) -> LLMSettings:
    return LLMSettings(api_key="secret-key", base_url=base_url, model=model)


def test_discovery_reads_deepseek_context_window(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_read(url: str, api_key: str, timeout: float) -> Any:
        calls.append(url)
        return {
            "object": "list",
            "data": [
                {"id": "other", "context_window": 999},
                {
                    "id": "deepseek-flash",
                    "name": "DeepSeek-V4.1-Flash",
                    "context_window": 1_048_576,
                    "max_output_tokens": 393_216,
                },
            ],
        }

    monkeypatch.setattr("core.model_metadata._read_json", fake_read)
    metadata = discover_model_metadata(_settings(model="deepseek-flash"))

    assert metadata.context_window == 1_048_576
    assert metadata.max_output_tokens == 393_216
    assert metadata.display_name == "DeepSeek-V4.1-Flash"
    assert metadata.available
    assert metadata.source == "上游"
    assert calls == ["https://api.deepseek.com/v1/models"]


def test_discovery_normalizes_uppercase_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_read(url: str, api_key: str, timeout: float) -> Any:
        calls.append(url)
        return {"data": [{"id": "m", "context_length": 8_192}]}

    monkeypatch.setattr("core.model_metadata._read_json", fake_read)
    metadata = discover_model_metadata(
        _settings(base_url="https://api.DeepSeek.com/v1")
    )

    assert metadata.context_window == 8_192
    assert calls[0] == "https://api.deepseek.com/v1/models"


def test_discovery_reads_nested_provider_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.model_metadata._read_json",
        lambda *_args: {
            "data": [
                {
                    "id": "m",
                    "top_provider": {"context_length": 200_000},
                    "max_completion_tokens": 8_000,
                }
            ]
        },
    )
    metadata = discover_model_metadata(_settings())
    assert metadata.context_window == 200_000
    assert metadata.max_output_tokens == 8_000


def test_discovery_falls_back_to_model_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_read(url: str, api_key: str, timeout: float) -> Any:
        if url.endswith("/models"):
            return {"data": [{"id": "m", "owned_by": "test"}]}
        return {"id": "m", "max_model_len": 32_768}

    monkeypatch.setattr("core.model_metadata._read_json", fake_read)
    metadata = discover_model_metadata(_settings())
    assert metadata.context_window == 32_768
    assert metadata.endpoint.endswith("/models/m")


def test_discovery_reports_missing_context_without_faking_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.model_metadata._read_json",
        lambda *_args: {"data": [{"id": "m", "owned_by": "test"}]},
    )
    metadata = discover_model_metadata(_settings())
    assert metadata.context_window is None
    assert metadata.available is False
    assert "context_window" in metadata.error


class _ModelsHandler(BaseHTTPRequestHandler):
    calls: list[tuple[str, str | None]] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.__class__.calls.append(
            (self.path, self.headers.get("Authorization"))
        )
        if self.path == "/v1/models":
            payload = {
                "object": "list",
                "data": [
                    {
                        "id": "deepseek-flash",
                        "name": "DeepSeek-V4.1-Flash",
                        "context_window": 1_048_576,
                        "max_output_tokens": 393_216,
                    }
                ],
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:
        return


def test_discovery_uses_real_http_upstream() -> None:
    _ModelsHandler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        settings = LLMSettings(
            api_key="secret-key",
            base_url=f"http://127.0.0.1:{port}/v1",
            model="deepseek-flash",
        )
        metadata = discover_model_metadata(settings, timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert metadata.context_window == 1_048_576
    assert metadata.endpoint == f"http://127.0.0.1:{port}/v1/models"
    assert _ModelsHandler.calls == [
        ("/v1/models", "Bearer secret-key"),
    ]
    assert "secret-key" not in repr(metadata)


def test_discovery_error_does_not_leak_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: Any) -> Any:
        raise _FetchError("无法连接上游：connection refused")

    monkeypatch.setattr("core.model_metadata._read_json", fail)
    metadata = discover_model_metadata(_settings())
    assert metadata.context_window is None
    assert "secret-key" not in repr(metadata)
    assert "secret-key" not in metadata.error
    assert "无法连接上游" in metadata.error
