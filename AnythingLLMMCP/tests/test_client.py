"""``client.AnythingLLMClient``：请求形状、重试与 7 类错误映射。"""

from __future__ import annotations

import json

import httpx
import pytest

import client as client_module
from client import AnythingLLMClient, Settings
from errors import (
    ApiError,
    BackendTimeoutError,
    BackendUnavailableError,
    ConfigurationError,
    InvalidResponseError,
    ValidationError,
)

from .conftest import json_response


def test_settings_requires_api_key() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        Settings.from_env({"ANYTHINGLLM_URL": "http://localhost:3001"})
    assert excinfo.value.code == "CONFIG_ERROR"
    assert "ANYTHINGLLM_API_KEY" in str(excinfo.value)


def test_settings_reads_env_and_masks_key() -> None:
    settings = Settings.from_env(
        {
            "ANYTHINGLLM_API_KEY": "sk-test",
            "ANYTHINGLLM_URL": "http://llm:3001/",
            "ANYTHINGLLM_TIMEOUT": "12",
            "ANYTHINGLLM_MAX_RETRIES": "3",
        }
    )
    assert settings.base_url == "http://llm:3001"
    assert settings.timeout == 12.0
    assert settings.max_retries == 3
    assert "sk-test" not in repr(settings)


async def test_list_workspaces_sends_bearer(client, upstream) -> None:
    upstream.responses = [json_response({"workspaces": [{"name": "kb", "slug": "kb"}]})]

    workspaces = await client.list_workspaces()

    assert workspaces == [{"name": "kb", "slug": "kb"}]
    assert upstream.last.method == "GET"
    assert upstream.last.url.path == "/api/v1/workspaces"
    assert upstream.last.headers["authorization"] == "Bearer test-key"


async def test_missing_workspaces_key_is_invalid_response(client, upstream) -> None:
    upstream.responses = [json_response({"data": []})]
    with pytest.raises(InvalidResponseError):
        await client.list_workspaces()


async def test_http_error_maps_to_api_error_with_status(client, upstream) -> None:
    upstream.responses = [json_response({"error": "workspace not found"}, status=404)]
    with pytest.raises(ApiError) as excinfo:
        await client.get_workspace("kb")
    assert excinfo.value.details["status"] == 404
    assert "workspace not found" in excinfo.value.details["upstream_message"]


async def test_timeout_maps_to_backend_timeout(upstream, settings) -> None:
    def raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    instance = AnythingLLMClient(
        settings, transport=httpx.MockTransport(raise_timeout)
    )
    with pytest.raises(BackendTimeoutError):
        await instance.list_workspaces()


async def test_transport_error_maps_to_unavailable(settings) -> None:
    def raise_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    instance = AnythingLLMClient(settings, transport=httpx.MockTransport(raise_connect))
    with pytest.raises(BackendUnavailableError):
        await instance.list_workspaces()


async def test_retries_5xx_then_succeeds(settings) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(200, json={"workspaces": []})

    instance = AnythingLLMClient(
        Settings(base_url="http://x", api_key="k", max_retries=1, retry_backoff=0.0),
        transport=httpx.MockTransport(handler),
    )
    assert await instance.list_workspaces() == []
    assert len(calls) == 2


async def test_create_workspace_posts_name(client, upstream) -> None:
    upstream.responses = [json_response({"workspace": {"name": "新库", "slug": "new-kb"}})]
    workspace = await client.create_workspace("新库")
    assert workspace["slug"] == "new-kb"
    assert upstream.last.method == "POST"
    assert upstream.last.url.path == "/api/v1/workspace/new"
    assert b'"name"' in upstream.last.content


async def test_get_workspace_accepts_list_payload(client, upstream) -> None:
    """AnythingLLM 1.x 返回数组、2.x 返回对象，两种都要能解析。"""
    upstream.responses = [json_response({"workspace": [{"slug": "kb", "documents": []}]})]
    workspace = await client.get_workspace("kb")
    assert workspace["slug"] == "kb"


async def test_upload_text_posts_raw_text(client, upstream) -> None:
    upstream.responses = [
        json_response({"success": True, "documents": [{"id": "doc-1", "title": "规范"}]})
    ]
    payload = await client.upload_text("正文", "规范", add_to_workspaces=["kb", "kb2"])
    assert payload["documents"][0]["id"] == "doc-1"

    request = upstream.last
    assert request.url.path == "/api/v1/document/raw-text"
    body = json.loads(request.content)
    assert body["textContent"] == "正文"
    assert body["addToWorkspaces"] == "kb,kb2"


async def test_upload_text_success_false_raises(client, upstream) -> None:
    upstream.responses = [json_response({"success": False, "error": "unsupported type"})]
    with pytest.raises(ApiError) as excinfo:
        await client.upload_text("正文", "规范")
    assert "unsupported type" in str(excinfo.value)


async def test_upload_file_rejects_missing_file(client, tmp_path) -> None:
    with pytest.raises(ValidationError):
        await client.upload_file(tmp_path / "nope.md")


async def test_upload_file_rejects_oversize(client, tmp_path, monkeypatch) -> None:
    target = tmp_path / "big.md"
    target.write_bytes(b"x" * 10)
    monkeypatch.setattr(client_module, "MAX_UPLOAD_BYTES", 5)
    with pytest.raises(ValidationError) as excinfo:
        await client.upload_file(target)
    assert excinfo.value.code == "VALIDATION_ERROR"
    assert excinfo.value.details["path"].endswith("big.md")


async def test_upload_file_sends_multipart(client, upstream, tmp_path) -> None:
    target = tmp_path / "doc.md"
    target.write_bytes(b"# hello")
    upstream.responses = [json_response({"success": True, "documents": [{"id": "d1"}]})]

    await client.upload_file(target, add_to_workspaces=["kb"])

    assert upstream.last.url.path == "/api/v1/document/upload"
    assert "multipart/form-data" in upstream.last.headers["content-type"]
    assert b"doc.md" in upstream.last.content


async def test_vectorize_uses_update_embeddings(client, upstream) -> None:
    upstream.responses = [json_response({"success": True, "workspace": {"slug": "kb"}})]
    await client.vectorize("kb")
    assert upstream.last.url.path == "/api/v1/workspace/kb/update-embeddings"
    assert upstream.last.method == "POST"


async def test_chat_returns_payload_and_passes_mode(client, upstream) -> None:
    upstream.responses = [
        json_response({"id": "sess-1", "textResponse": "答案", "sources": []})
    ]
    payload = await client.chat("kb", "问题", mode="chat", session_id="sess-1")
    assert payload["textResponse"] == "答案"

    body = json.loads(upstream.last.content)
    assert body["mode"] == "chat"
    assert body["sessionId"] == "sess-1"


async def test_chat_error_field_raises(client, upstream) -> None:
    upstream.responses = [json_response({"error": "Workspace not found"})]
    with pytest.raises(ApiError):
        await client.chat("kb", "问题")


async def test_non_json_response_is_invalid(client, upstream) -> None:
    upstream.responses = [httpx.Response(200, text="<html>oops</html>")]
    with pytest.raises(InvalidResponseError):
        await client.list_workspaces()
