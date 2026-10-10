import json
import os

import httpx
from mcp.server.mcpserver import MCPServer

BASE_URL = os.getenv("ANYTHINGLLM_URL", "http://localhost:3001").rstrip("/")
_API_KEY = os.getenv("ANYTHINGLLM_API_KEY")
if not _API_KEY:
    raise RuntimeError(
        "缺少环境变量 ANYTHINGLLM_API_KEY。请在 AnythingLLM 设置页生成 API Key，"
        "然后将其写入本项目的 .env 或系统环境变量后再启动服务。"
    )
HEADERS = {"Authorization": "Bearer " + _API_KEY}

mcp = MCPServer("anythingllm-workspace-files")


@mcp.tool()
async def get_first_workspace_files() -> str:
    """获取 AnythingLLM 中第一个工作区内的所有文件信息（名称、来源路径、大小、时间等）。"""
    async with httpx.AsyncClient(base_url=BASE_URL, headers=HEADERS, timeout=60.0) as c:
        r = await c.get("/api/v1/workspaces")
        r.raise_for_status()
        workspaces = r.json()["workspaces"]
        if not workspaces:
            return json.dumps({"error": "AnythingLLM 中没有工作区"}, ensure_ascii=False)

        r = await c.get(f"/api/v1/workspace/{workspaces[0]['slug']}")
        r.raise_for_status()
        ws = r.json()["workspace"]
        if isinstance(ws, list):
            ws = ws[0] if ws else {}

    files = []
    for doc in ws.get("documents") or []:
        meta = {}
        try:
            meta = json.loads(doc.get("metadata") or "{}")
        except json.JSONDecodeError:
            pass
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

    return json.dumps(
        {
            "workspace": {"name": ws.get("name"), "slug": ws.get("slug"), "id": ws.get("id")},
            "fileCount": len(files),
            "files": files,
        },
        ensure_ascii=False,
        indent=2,
    )


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8080")),
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )
