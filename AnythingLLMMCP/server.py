"""AnythingLLM MCP Server（Streamable HTTP，无状态）。

把 AnythingLLM 的三类能力封装成 MCP 工具：

* **工作区管理**：``list_workspaces`` / ``create_workspace``；
* **文档与向量化**：``list_workspace_documents`` / ``upload_text_document`` /
  ``upload_local_file`` / ``vectorize_workspace``；
* **检索问答**：``chat_workspace``（``mode=query`` 只检索，``mode=chat`` 带对话历史）。

运行::

    set ANYTHINGLLM_API_KEY=...        # 必填
    python server.py                   # 默认 127.0.0.1:8080/mcp

失败时工具结果是 ``isError: true``，文本为统一错误信封
``{"error": {"code", "message", "details"}}``。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from client import AnythingLLMClient, Settings
from errors import AnythingLLMError, ConfigurationError, InternalError, ValidationError

try:  # SDK 2.x 的 ToolError 位置；缺失时退化为普通异常（SDK 仍会转成 isError）
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - 兼容性兜底
    class ToolError(Exception):  # type: ignore[no-redef]
        """本地兜底：只要抛出异常，SDK 就会把它转成 isError 结果。"""

from mcp.server.mcpserver import MCPServer

__all__ = ["TOOL_NAMES", "main", "mcp"]

logger = logging.getLogger(__name__)

SERVER_NAME = "anythingllm-workspace"
SLUG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
"""AnythingLLM 的 slug / 工作区标识：只允许字母、数字、连字符和下划线。"""

TOOL_NAMES: tuple[str, ...] = (
    "list_workspaces",
    "create_workspace",
    "list_workspace_documents",
    "upload_text_document",
    "upload_local_file",
    "vectorize_workspace",
    "chat_workspace",
)
TOOL_COUNT = len(TOOL_NAMES)

MAX_TEXT_CHARS = 2_000_000
MAX_TITLE_CHARS = 200
MAX_MESSAGE_CHARS = 8_000

mcp = MCPServer(SERVER_NAME)

_client_singleton: AnythingLLMClient | None = None


# ---------------------------------------------------------------------------
# 客户端与错误处理
# ---------------------------------------------------------------------------
def _client() -> AnythingLLMClient:
    """惰性创建客户端：让服务在缺少 Key 时也能启动并给出结构化错误。"""
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = AnythingLLMClient(Settings.from_env())
    return _client_singleton


@contextlib.asynccontextmanager
async def _guard(tool_name: str):
    """把预期异常转成统一错误信封，把意外异常收成 INTERNAL_ERROR。"""
    try:
        yield
    except ToolError:
        # 工具函数已经按信封格式抛出的错误，原样透传，不能再包成 INTERNAL_ERROR。
        raise
    except AnythingLLMError as exc:
        logger.warning("tool_failed", extra={"tool_name": tool_name, "error_code": exc.code})
        raise ToolError(exc.to_json()) from exc
    except Exception as exc:  # 兜底：原始异常文本不流向客户端
        logger.exception("tool_crashed", extra={"tool_name": tool_name})
        raise ToolError(
            InternalError(details={"reason": type(exc).__name__, "tool": tool_name}).to_json()
        ) from exc


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _invalid(message: str, *, field: str) -> ToolError:
    """构造已经带统一信封的入参错误，交给 ``_guard`` 原样透传。"""
    return ToolError(ValidationError(message, details={"field": field}).to_json())


def _check_slug(value: str, label: str = "workspace_slug") -> str:
    text = (value or "").strip()
    if not re.fullmatch(SLUG_PATTERN, text):
        raise _invalid(
            f"{label} 不合法：{text!r}；只允许字母、数字、连字符和下划线，长度 1-64",
            field=label,
        )
    return text


def _normalize_slugs(values: list[str] | None, label: str = "add_to_workspaces") -> list[str]:
    if not values:
        return []
    seen: list[str] = []
    for value in values:
        slug = _check_slug(value, label)
        if slug not in seen:
            seen.append(slug)
    return seen


# ---------------------------------------------------------------------------
# 1. 工作区管理
# ---------------------------------------------------------------------------
@mcp.tool()
async def list_workspaces() -> str:
    """列出 AnythingLLM 的全部工作区（名称、slug、ID、文档数）。

    当用户问“有哪些知识库 / 工作区”“我的文档在哪个工作区”，或需要先拿到 slug
    再调用其他工具时使用。返回 JSON 文本：
    `{"count": 2, "workspaces": [{"name", "slug", "id", "documentCount"}]}`。
    工作区为空时 `count` 为 0，不是错误。
    """
    async with _guard("list_workspaces"):
        workspaces = await _client().list_workspaces()

    items = [
        {
            "name": item.get("name"),
            "slug": item.get("slug"),
            "id": item.get("id"),
            "documentCount": len(item.get("documents") or []),
        }
        for item in workspaces
    ]
    return _dump({"count": len(items), "workspaces": items})


@mcp.tool()
async def create_workspace(
    name: Annotated[
        str,
        Field(min_length=1, max_length=64, description="工作区显示名称，例如「宠物医院知识库」"),
    ],
) -> str:
    """创建一个新的 AnythingLLM 工作区。

    当用户要求“新建一个知识库 / 工作区”时使用。返回 JSON 文本，包含新工作区的
    `name` / `slug` / `id`；后续上传文档、向量化、问答都用这里返回的 `slug`。
    同名工作区已存在时，AnythingLLM 会返回它自己的错误，工具会原样转成
    API_ERROR 并给出上游说明。
    """
    title = (name or "").strip()
    if not title:
        raise _invalid("name 不能为空", field="name")

    async with _guard("create_workspace"):
        workspace = await _client().create_workspace(title)

    return _dump(
        {
            "name": workspace.get("name"),
            "slug": workspace.get("slug"),
            "id": workspace.get("id"),
            "message": "工作区已创建；下一步可用 upload_text_document 上传文档，"
            "再调用 vectorize_workspace 让新文档可被检索。",
        }
    )


# ---------------------------------------------------------------------------
# 2. 文档与向量化
# ---------------------------------------------------------------------------
@mcp.tool()
async def list_workspace_documents(
    workspace_slug: Annotated[
        str | None,
        Field(
            pattern=SLUG_PATTERN,
            description="工作区 slug；省略时使用第一个工作区，并在返回里注明实际使用的 slug",
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, le=500, description="最多返回多少个文档，默认 100"),
    ] = 100,
) -> str:
    """列出某个工作区里的文档元数据（标题、文件名、docId、来源、词数、token 数、时间）。

    当用户问“知识库里有哪些文件”“这个工作区收了什么资料”时使用。只读取元数据，
    不读取文档正文。返回 JSON 文本：
    `{"workspace": {...}, "fileCount", "returned", "truncated", "files": [...]}`。
    `workspace_slug` 省略时使用第一个工作区。
    """
    async with _guard("list_workspace_documents"):
        client = _client()
        slug = (workspace_slug or "").strip()
        if slug:
            slug = _check_slug(slug)
        else:
            workspaces = await client.list_workspaces()
            if not workspaces:
                raise ValidationError(
                    "AnythingLLM 中还没有任何工作区，请先用 create_workspace 创建一个"
                )
            slug = str(workspaces[0].get("slug") or "")
        workspace = await client.get_workspace(slug)

    files = []
    for doc in workspace.get("documents") or []:
        if not isinstance(doc, dict):
            continue
        meta = _parse_metadata(doc.get("metadata"))
        files.append(
            {
                "title": meta.get("title"),
                "filename": doc.get("filename"),
                "docId": doc.get("docId"),
                "chunkSource": meta.get("chunkSource") or meta.get("url"),
                "docSource": meta.get("docSource"),
                "wordCount": meta.get("wordCount"),
                "tokenCount": meta.get("token_count_estimate"),
                "published": meta.get("published"),
                "pinned": doc.get("pinned"),
                "createdAt": doc.get("createdAt"),
                "lastUpdatedAt": doc.get("lastUpdatedAt"),
            }
        )

    returned = files[:limit]
    return _dump(
        {
            "workspace": {
                "name": workspace.get("name"),
                "slug": workspace.get("slug") or slug,
                "id": workspace.get("id"),
            },
            "fileCount": len(files),
            "returned": len(returned),
            "truncated": len(files) > len(returned),
            "files": returned,
        }
    )


@mcp.tool()
async def upload_text_document(
    text: Annotated[
        str,
        Field(min_length=1, description="要写入知识库的纯文本内容（Markdown / 纯文本）"),
    ],
    title: Annotated[
        str,
        Field(min_length=1, max_length=MAX_TITLE_CHARS, description="文档标题，便于以后检索"),
    ],
    add_to_workspaces: Annotated[
        list[str] | None,
        Field(
            description=(
                "要加入的工作区 slug 列表；省略时只上传到 AnythingLLM 的文档库，"
                "不加入任何工作区"
            )
        ),
    ] = None,
) -> str:
    """把一段文本作为新文档上传到 AnythingLLM，并可直接加入一个或多个工作区。

    当用户要求“把这段内容/这份总结存进知识库”“上传这份 Markdown”时使用。
    上传后文档需要向量化才能被检索：传了 `add_to_workspaces` 时通常会由
    AnythingLLM 自动处理，仍建议随后调用 `vectorize_workspace` 确认。
    返回 JSON 文本，包含 `documents`（新文档的 id / 标题）、`workspaces` 与
    `nextStep` 提示。
    """
    content = text or ""
    if not content.strip():
        raise _invalid("text 不能为空", field="text")
    if len(content) > MAX_TEXT_CHARS:
        raise _invalid(
            f"text 过长（{len(content)} 字符），超过 {MAX_TEXT_CHARS} 字符上限",
            field="text",
        )
    name = (title or "").strip()
    if not name:
        raise _invalid("title 不能为空", field="title")

    async with _guard("upload_text_document"):
        slugs = _normalize_slugs(add_to_workspaces)
        payload = await _client().upload_text(content, name, add_to_workspaces=slugs)

    return _dump(
        {
            "success": payload.get("success", True),
            "documents": [
                {
                    "id": doc.get("id"),
                    "title": (doc.get("title") or name),
                    "location": doc.get("location") or doc.get("url"),
                }
                for doc in (payload.get("documents") or [])
                if isinstance(doc, dict)
            ],
            "workspaces": slugs,
            "nextStep": "如需让文档可被检索，调用 vectorize_workspace 触发向量化。",
        }
    )


@mcp.tool()
async def upload_local_file(
    path: Annotated[
        str,
        Field(
            min_length=1,
            description="本机上要上传的文件路径（绝对路径或相对于服务进程工作目录）",
        ),
    ],
    add_to_workspaces: Annotated[
        list[str] | None,
        Field(description="要加入的工作区 slug 列表；省略时只上传到文档库"),
    ] = None,
) -> str:
    """把本机的一个文件（PDF / Markdown / TXT / DOCX 等）上传到 AnythingLLM。

    当用户说“把这个文件传进知识库”并给出本机路径时使用。文件由 **MCP 服务所在的
    机器** 读取，因此路径必须是那台机器上的真实文件；单文件上限 20 MB。
    返回 JSON 文本，包含文档 id、标题、文件大小与下一步提示。
    """
    raw = (path or "").strip()
    if not raw:
        raise _invalid("path 不能为空", field="path")
    if "\x00" in raw:
        raise _invalid("path 包含空字符", field="path")

    target = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not target.is_absolute():
        target = Path.cwd() / target

    async with _guard("upload_local_file"):
        slugs = _normalize_slugs(add_to_workspaces)
        size = target.stat().st_size if target.exists() else 0
        payload = await _client().upload_file(target, add_to_workspaces=slugs)

    return _dump(
        {
            "success": payload.get("success", True),
            "path": str(target),
            "size": size,
            "documents": [
                {
                    "id": doc.get("id"),
                    "title": doc.get("title") or doc.get("name"),
                    "location": doc.get("location") or doc.get("url"),
                }
                for doc in (payload.get("documents") or [])
                if isinstance(doc, dict)
            ],
            "workspaces": slugs,
            "nextStep": "如需让文档可被检索，调用 vectorize_workspace 触发向量化。",
        }
    )


@mcp.tool()
async def vectorize_workspace(
    workspace_slug: Annotated[
        str,
        Field(pattern=SLUG_PATTERN, description="要重新向量化的工作区 slug"),
    ],
) -> str:
    """触发某个工作区的「重新向量化」（update-embeddings）。

    当用户说“让新文档可被检索”“重建索引”“向量化这个知识库”，或上传文档后
    需要确认索引状态时使用。返回 JSON 文本，包含工作区名称、文档数与完成提示；
    向量化在 AnythingLLM 侧可能是后台任务，大的知识库需要稍等再提问。
    """
    slug = _check_slug(workspace_slug)

    async with _guard("vectorize_workspace"):
        payload = await _client().vectorize(slug)

    workspace = payload.get("workspace")
    if isinstance(workspace, list):
        workspace = workspace[0] if workspace else {}
    workspace = workspace if isinstance(workspace, dict) else {}
    return _dump(
        {
            "workspace": {
                "name": workspace.get("name"),
                "slug": workspace.get("slug") or slug,
            },
            "message": payload.get("message") or "已触发向量化",
            "documentCount": len(workspace.get("documents") or []),
        }
    )


# ---------------------------------------------------------------------------
# 3. 检索问答
# ---------------------------------------------------------------------------
@mcp.tool()
async def chat_workspace(
    workspace_slug: Annotated[
        str,
        Field(pattern=SLUG_PATTERN, description="要提问的工作区 slug"),
    ],
    message: Annotated[
        str,
        Field(min_length=1, max_length=MAX_MESSAGE_CHARS, description="用户的问题"),
    ],
    mode: Annotated[
        Literal["query", "chat"],
        Field(
            description=(
                "query=只基于该工作区的文档片段回答（默认，适合知识库问答）；"
                "chat=允许结合该会话的历史上下文回答"
            )
        ),
    ] = "query",
    session_id: Annotated[
        str | None,
        Field(
            max_length=64,
            description="可选，多轮对话的会话 ID；同一 ID 的多次调用共享上下文",
        ),
    ] = None,
) -> str:
    """基于某个工作区的知识库回答问题（AnythingLLM 检索问答）。

    当用户问“根据知识库回答……”“我的文档里是怎么写的”“在某个工作区里查一下”
    时使用。`mode=query` 只依据文档片段作答，`mode=chat` 会带上该会话的历史。
    返回 JSON 文本，包含 `textResponse`（回答正文）、`sources`（引用片段来源）
    与 `sessionId`（需要多轮时把它带到下一次调用）。
    """
    slug = _check_slug(workspace_slug)
    question = (message or "").strip()
    if not question:
        raise _invalid("message 不能为空", field="message")

    async with _guard("chat_workspace"):
        payload = await _client().chat(
            slug,
            question,
            mode=mode,
            session_id=(session_id or "").strip() or None,
        )

    sources = []
    for source in payload.get("sources") or []:
        if not isinstance(source, dict):
            continue
        sources.append(
            {
                "title": source.get("title"),
                "chunk": source.get("chunk"),
                "score": source.get("score"),
            }
        )

    return _dump(
        {
            "workspace": {"slug": slug},
            "mode": mode,
            "textResponse": payload.get("textResponse"),
            "sources": sources,
            "sourceCount": len(sources),
            "sessionId": payload.get("id") or session_id,
            "close": payload.get("close"),
        }
    )


# ---------------------------------------------------------------------------
# 辅助与入口
# ---------------------------------------------------------------------------
def _parse_metadata(value: Any) -> dict[str, Any]:
    """AnythingLLM 的 ``metadata`` 是 JSON 字符串；坏数据按空对象处理。"""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def main() -> int:
    """命令行入口：先校验配置，再启动无状态 Streamable HTTP 服务。"""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)r}',
    )
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(exc.to_json(), file=sys.stderr)
        return 2

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    logger.info(
        "starting: base_url=%s host=%s port=%s endpoint=/mcp tools=%d",
        settings.base_url,
        host,
        port,
        TOOL_COUNT,
    )
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
