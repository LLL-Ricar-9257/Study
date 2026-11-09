"""MCP 工具层：入参校验、真实请求形状、结构化返回与错误信封。

这里的测试直接调用 ``server`` 模块里的工具函数（MCP SDK 用它们生成 schema），
并通过 ``httpx.MockTransport`` 断言真实发出的 HTTP 请求，不访问任何外部服务。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import server as server_module
from client import AnythingLLMClient, Settings
from server import (
    TOOL_NAMES,
    chat_workspace,
    create_workspace,
    list_workspace_documents,
    list_workspaces,
    upload_local_file,
    upload_text_document,
    vectorize_workspace,
)

from .conftest import json_response


@pytest.fixture
def srv(settings: Settings, upstream, monkeypatch):
    """把模块级客户端替换成走 MockTransport 的实例。"""
    instance = AnythingLLMClient(settings, transport=httpx.MockTransport(upstream.handler))
    monkeypatch.setattr(server_module, "_client_singleton", instance)
    return server_module


def error_payload(exc: BaseException) -> dict[str, Any]:
    text = str(exc)
    return json.loads(text[text.find("{"):])


def test_tool_names_cover_three_capability_classes() -> None:
    assert TOOL_NAMES == (
        "list_workspaces",
        "create_workspace",
        "list_workspace_documents",
        "upload_text_document",
        "upload_local_file",
        "vectorize_workspace",
        "chat_workspace",
    )


# ---------------------------------------------------------------------------
# 工作区
# ---------------------------------------------------------------------------
async def test_list_workspaces_returns_counts(srv, upstream) -> None:
    upstream.responses = [
        json_response(
            {
                "workspaces": [
                    {"name": "宠物医院知识库", "slug": "pet-hospital", "id": 1,
                     "documents": [{"docId": "d1"}, {"docId": "d2"}]},
                    {"name": "空库", "slug": "empty", "id": 2},
                ]
            }
        )
    ]

    payload = json.loads(await list_workspaces())

    assert payload["count"] == 2
    assert payload["workspaces"][0]["documentCount"] == 2
    assert payload["workspaces"][1]["documentCount"] == 0


async def test_create_workspace_returns_slug(srv, upstream) -> None:
    upstream.responses = [
        json_response({"workspace": {"name": "新库", "slug": "new-kb", "id": 3}})
    ]

    payload = json.loads(await create_workspace("新库"))

    assert payload["slug"] == "new-kb"
    assert "vectorize_workspace" in payload["message"]
    assert json.loads(upstream.last.content)["name"] == "新库"


async def test_create_workspace_rejects_blank_name(srv, upstream) -> None:
    with pytest.raises(Exception) as excinfo:
        await create_workspace("   ")
    assert error_payload(excinfo.value)["error"]["code"] == "VALIDATION_ERROR"
    assert upstream.call_count == 0


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------
async def test_documents_default_to_first_workspace(srv, upstream) -> None:
    upstream.responses = [
        json_response({"workspaces": [{"slug": "first-kb", "name": "第一个"}]}),
        json_response(
            {
                "workspace": {
                    "name": "第一个",
                    "slug": "first-kb",
                    "documents": [
                        {
                            "docId": "d1",
                            "filename": "handbook.pdf",
                            "metadata": json.dumps(
                                {"title": "手册", "wordCount": 1200, "chunkSource": "upload"}
                            ),
                        }
                    ],
                }
            }
        ),
    ]

    payload = json.loads(await list_workspace_documents())

    assert payload["workspace"]["slug"] == "first-kb"
    assert payload["fileCount"] == 1
    assert payload["files"][0]["title"] == "手册"
    assert payload["files"][0]["chunkSource"] == "upload"
    assert upstream.requests[1].url.path == "/api/v1/workspace/first-kb"


async def test_documents_limit_and_truncated_flag(srv, upstream) -> None:
    docs = [{"docId": f"d{i}", "metadata": "{}"} for i in range(5)]
    upstream.responses = [
        json_response({"workspace": {"slug": "kb", "documents": docs}})
    ]

    payload = json.loads(await list_workspace_documents("kb", limit=2))

    assert payload["fileCount"] == 5
    assert payload["returned"] == 2
    assert payload["truncated"] is True


async def test_documents_without_any_workspace_is_validation_error(srv, upstream) -> None:
    upstream.responses = [json_response({"workspaces": []})]
    with pytest.raises(Exception) as excinfo:
        await list_workspace_documents()
    assert error_payload(excinfo.value)["error"]["code"] == "VALIDATION_ERROR"


async def test_upload_text_document_adds_to_workspaces(srv, upstream) -> None:
    upstream.responses = [
        json_response(
            {"success": True, "documents": [{"id": "doc-9", "title": "规范", "location": "custom-documents/x.json"}]}
        )
    ]

    payload = json.loads(
        await upload_text_document("正文", "规范", add_to_workspaces=["kb", "kb2"])
    )

    assert payload["documents"][0]["id"] == "doc-9"
    assert payload["workspaces"] == ["kb", "kb2"]
    assert "vectorize_workspace" in payload["nextStep"]

    request = upstream.last
    assert request.url.path == "/api/v1/document/raw-text"
    body = json.loads(request.content)
    assert body["textContent"] == "正文"
    assert body["addToWorkspaces"] == "kb,kb2"
    assert body["metadata"]["title"] == "规范"


async def test_upload_text_document_deduplicates_workspaces(srv, upstream) -> None:
    upstream.responses = [json_response({"success": True, "documents": []})]
    payload = json.loads(
        await upload_text_document("正文", "规范", add_to_workspaces=["kb", "kb"])
    )
    assert payload["workspaces"] == ["kb"]


@pytest.mark.parametrize("bad", ["../etc", "a/b", "", "x" * 70])
async def test_upload_document_rejects_bad_workspace_slug(srv, upstream, bad: str) -> None:
    upstream.responses = [json_response({"success": True, "documents": []})]
    with pytest.raises(Exception) as excinfo:
        await upload_text_document("正文", "规范", add_to_workspaces=[bad])
    assert error_payload(excinfo.value)["error"]["code"] == "VALIDATION_ERROR"
    assert upstream.call_count == 0


async def test_upload_text_document_rejects_empty_text(srv, upstream) -> None:
    with pytest.raises(Exception) as excinfo:
        await upload_text_document("   ", "规范")
    assert error_payload(excinfo.value)["error"]["code"] == "VALIDATION_ERROR"
    assert upstream.call_count == 0


async def test_upload_local_file_rejects_missing_path(srv, upstream, tmp_path) -> None:
    with pytest.raises(Exception) as excinfo:
        await upload_local_file(str(tmp_path / "nope.md"))
    assert error_payload(excinfo.value)["error"]["code"] == "VALIDATION_ERROR"
    assert upstream.call_count == 0


async def test_upload_local_file_sends_multipart(srv, upstream, tmp_path) -> None:
    target = tmp_path / "handbook.md"
    target.write_text("# 手册", encoding="utf-8")
    upstream.responses = [
        json_response({"success": True, "documents": [{"id": "d1", "title": "handbook.md"}]})
    ]

    payload = json.loads(await upload_local_file(str(target), add_to_workspaces=["kb"]))

    assert payload["path"] == str(target)
    assert payload["size"] == len("# 手册".encode())
    assert payload["documents"][0]["id"] == "d1"
    assert "multipart/form-data" in upstream.last.headers["content-type"]


async def test_vectorize_calls_update_embeddings(srv, upstream) -> None:
    upstream.responses = [
        json_response({"success": True, "message": "Vectorization started", "workspace": {"slug": "kb", "documents": [{}, {}]}})
    ]

    payload = json.loads(await vectorize_workspace("kb"))

    assert payload["documentCount"] == 2
    assert payload["message"] == "Vectorization started"
    assert upstream.last.url.path == "/api/v1/workspace/kb/update-embeddings"


# ---------------------------------------------------------------------------
# 检索问答
# ---------------------------------------------------------------------------
async def test_chat_workspace_returns_answer_and_sources(srv, upstream) -> None:
    upstream.responses = [
        json_response(
            {
                "id": "sess-1",
                "type": "textResponse",
                "textResponse": "先登记主人电话。",
                "sources": [{"title": "接诊规范", "chunk": "先登记主人电话", "score": 0.9}],
                "close": False,
            }
        )
    ]

    payload = json.loads(
        await chat_workspace("pet-hospital", "第一步要做什么？", mode="query")
    )

    assert payload["textResponse"] == "先登记主人电话。"
    assert payload["sourceCount"] == 1
    assert payload["sources"][0]["title"] == "接诊规范"
    assert payload["sessionId"] == "sess-1"

    body = json.loads(upstream.last.content)
    assert body["mode"] == "query"
    assert body["message"] == "第一步要做什么？"


async def test_chat_workspace_rejects_bad_slug(srv, upstream) -> None:
    with pytest.raises(Exception) as excinfo:
        await chat_workspace("bad/slug", "问题")
    assert error_payload(excinfo.value)["error"]["code"] == "VALIDATION_ERROR"
    assert upstream.call_count == 0


async def test_chat_workspace_rejects_empty_message(srv, upstream) -> None:
    with pytest.raises(Exception) as excinfo:
        await chat_workspace("kb", "   ")
    assert error_payload(excinfo.value)["error"]["code"] == "VALIDATION_ERROR"
    assert upstream.call_count == 0


async def test_upstream_failure_becomes_api_error_envelope(srv, upstream) -> None:
    upstream.responses = [json_response({"error": "Workspace not found"}, status=404)]
    with pytest.raises(Exception) as excinfo:
        await chat_workspace("kb", "问题")
    payload = error_payload(excinfo.value)
    assert payload["error"]["code"] == "API_ERROR"
    assert payload["error"]["details"]["status"] == 404


async def test_unexpected_crash_is_internal_error(srv, monkeypatch) -> None:
    class Exploding:
        async def list_workspaces(self) -> list[dict[str, Any]]:
            raise RuntimeError("secret internal detail")

    monkeypatch.setattr(server_module, "_client_singleton", Exploding())

    with pytest.raises(Exception) as excinfo:
        await list_workspaces()

    payload = error_payload(excinfo.value)
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["details"]["reason"] == "RuntimeError"
    assert "secret internal detail" not in str(excinfo.value)
