# AnythingLLM MCP Server

把 [AnythingLLM](https://anythingllm.com/) 的三类能力封装成 **MCP 工具**，供任意
MCP 客户端（opencode、Claude Desktop、本仓库的 `Agent/xAgent` 等）在对话中调用：
**工作区管理**、**文档上传与向量化**、**知识库检索问答**。

- 传输：**Streamable HTTP**（无状态，`json_response=True`），端点 `POST /mcp`
- 协议：MCP Python SDK `mcp>=2.2.0`（`mcp.server.mcpserver.MCPServer`）
- 目录结构：

```text
AnythingLLMMCP/
├── server.py          # MCP 装配与 7 个工具的声明（薄层）
├── client.py          # AnythingLLM REST 客户端：鉴权 / 超时 / 重试 / 错误归类
├── errors.py          # 错误码与统一错误信封
├── tests/             # 离线单测（httpx.MockTransport，不访问真实 AnythingLLM）
├── requirements.txt
└── opencode.json      # opencode 的 MCP 注册示例
```

---

## 1. 工具清单

| # | 工具 | 能力类 | 对应 AnythingLLM API | 真实副作用 |
| --- | --- | --- | --- | --- |
| 1 | `list_workspaces` | 工作区管理 | `GET /api/v1/workspaces` | 拉取真实工作区列表 |
| 2 | `create_workspace` | 工作区管理 | `POST /api/v1/workspace/new` | 在 AnythingLLM 中创建工作区 |
| 3 | `list_workspace_documents` | 文档 | `GET /api/v1/workspace/{slug}` | 读取该工作区文档元数据 |
| 4 | `upload_text_document` | 文档上传 | `POST /api/v1/document/raw-text` | 落库为新文档，可加入工作区 |
| 5 | `upload_local_file` | 文档上传 | `POST /api/v1/document/upload` | 读取本机文件并以 multipart 上传 |
| 6 | `vectorize_workspace` | 向量化 | `POST /api/v1/workspace/{slug}/update-embeddings` | 触发该工作区重新向量化 |
| 7 | `chat_workspace` | 检索问答 | `POST /api/v1/workspace/{slug}/chat` | 基于知识库片段生成回答 |

所有工具都支持 MCP 客户端的 `tools/list` 枚举；失败时返回统一信封：

```json
{"error": {"code": "API_ERROR", "message": "上传文件失败：...", "details": {"action": "上传文件"}}}
```

| 错误码 | 含义 |
| --- | --- |
| `CONFIG_ERROR` | 缺少 `ANYTHINGLLM_API_KEY` 等启动配置 |
| `VALIDATION_ERROR` | 工具入参非法（此时**不会**发请求） |
| `BACKEND_TIMEOUT` | 上游超时（已按配置重试） |
| `BACKEND_UNAVAILABLE` | 连不上 AnythingLLM |
| `API_ERROR` | HTTP 4xx/5xx 或 `success:false` / `error` 字段 |
| `INVALID_RESPONSE` | 响应不是合法 JSON 或缺少数必需字段 |
| `INTERNAL_ERROR` | 兜底，`details.reason` 给异常类型，不含堆栈 |

---

## 2. 配置

全部来自环境变量，**代码与仓库里没有任何密钥**：

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ANYTHINGLLM_API_KEY` | ✅ | — | 设置页 → API Keys 生成；缺失时启动即报 `CONFIG_ERROR` |
| `ANYTHINGLLM_URL` | ❌ | `http://localhost:3001` | AnythingLLM 实例地址 |
| `HOST` / `PORT` | ❌ | `127.0.0.1` / `8080` | 本 MCP 服务监听地址 |
| `ANYTHINGLLM_TIMEOUT` | ❌ | `60` | 单次请求超时（秒） |
| `ANYTHINGLLM_MAX_RETRIES` | ❌ | `1` | 超时 / 5xx / 429 的额外重试次数 |
| `ANYTHINGLLM_RETRY_BACKOFF` | ❌ | `0.5` | 首次退避秒数，之后按倍数递增 |
| `LOG_LEVEL` | ❌ | `INFO` | 日志级别 |

> ⚠️ 默认端口 `8080` 与仓库里 Go 宠物医院后端相同。两者同时运行时用 `PORT` 错开，
> 例如 `set PORT=8090`。

---

## 3. 启动

```bash
cd AnythingLLMMCP
pip install -r requirements.txt

# Windows（cmd）
set ANYTHINGLLM_API_KEY=your-key-here
set ANYTHINGLLM_URL=http://localhost:3001
python server.py

# PowerShell
$env:ANYTHINGLLM_API_KEY="your-key-here"
python server.py
```

启动后端点：`http://127.0.0.1:8080/mcp`。

在 **opencode** 中启用（`opencode.json` 已注册，`cwd` 用相对路径，换机器不用改）：

```json
{
  "mcp": {
    "anythingllm": {
      "type": "local",
      "cwd": "AnythingLLMMCP",
      "command": ["python", "server.py"],
      "enabled": true
    }
  }
}
```

在 **xAgent** 中启用（写进启动目录的 `.mcp.json`）：

```json
{
  "mcpServers": {
    "anythingllm": {
      "type": "streamable_http",
      "url": "http://127.0.0.1:8080/mcp"
    }
  }
}
```

---

## 4. 调用示例

### 4.1 先看有哪些工作区

```json
{"name": "list_workspaces", "arguments": {}}
```

```json
{
  "count": 1,
  "workspaces": [{"name": "宠物医院知识库", "slug": "pet-hospital", "id": 1, "documentCount": 12}]
}
```

### 4.2 上传一段文本并加入工作区

```json
{
  "name": "upload_text_document",
  "arguments": {
    "text": "# 接诊规范\n\n1. 先登记主人电话……",
    "title": "接诊规范",
    "add_to_workspaces": ["pet-hospital"]
  }
}
```

### 4.3 上传本机文件（PDF / Markdown / DOCX）

```json
{
  "name": "upload_local_file",
  "arguments": { "path": "D:\\docs\\handbook.pdf", "add_to_workspaces": ["pet-hospital"] }
}
```

> 文件由 **MCP 服务所在机器** 读取，单文件上限 20 MB；路径可以是绝对路径或相对
> 服务进程的工作目录。读不到时返回 `VALIDATION_ERROR`，不会静默成功。

### 4.4 向量化后提问

```json
{"name": "vectorize_workspace", "arguments": {"workspace_slug": "pet-hospital"}}
```

```json
{
  "name": "chat_workspace",
  "arguments": {
    "workspace_slug": "pet-hospital",
    "message": "接诊时第一步要登记什么？",
    "mode": "query"
  }
}
```

返回：

```json
{
  "workspace": {"slug": "pet-hospital"},
  "mode": "query",
  "textResponse": "先登记主人电话……",
  "sources": [{"title": "接诊规范", "chunk": "先登记主人电话", "score": 0.87}],
  "sourceCount": 1,
  "sessionId": "a1b2c3",
  "close": false
}
```

多轮对话时把返回的 `sessionId` 带到下一次 `chat_workspace` 调用（`mode=chat`）。

---

## 5. 失败场景与排查

| 现象 | 错误码 | 处理 |
| --- | --- | --- |
| 启动直接退出并打印 `CONFIG_ERROR` | `CONFIG_ERROR` | 没设 `ANYTHINGLLM_API_KEY`；设置后重开终端 |
| 工具返回 `BACKEND_UNAVAILABLE` | `BACKEND_UNAVAILABLE` | AnythingLLM 没启动，或 `ANYTHINGLLM_URL` 写错 / 端口不对 |
| 返回 `API_ERROR` + HTTP 401 | `API_ERROR` | Key 无效或已被吊销，重新生成 |
| 返回 `API_ERROR` + HTTP 404 | `API_ERROR` | 工作区 slug 不存在，先 `list_workspaces` 确认 |
| `upload_local_file` 报文件不存在 | `VALIDATION_ERROR` | 路径是 MCP 服务那台机器上的路径，注意反斜杠要转义 |
| `chat_workspace` 回答"没有找到相关信息" | — | 文档还没向量化，先 `vectorize_workspace` 再提问 |
| 端口被占用（`WinError 10048`） | — | 用 `PORT` 换一个端口，例如 `set PORT=8090` |

---

## 6. 测试

```bash
cd AnythingLLMMCP
pip install -r requirements-dev.txt
python -m pytest -q
ruff check .
```

测试全部走 `httpx.MockTransport`，**不需要真实 AnythingLLM**：覆盖 URL/鉴权头、
请求体形状、分页与截断、unknown-field 拒绝、7 类错误码映射。

---

## 7. 设计说明

- **分层**：`server.py` 只声明工具与入参校验，`client.py` 只做协议搬运，
  `errors.py` 只定义错误契约；新增工具不需要改客户端。
- **密钥边界**：`Settings.__repr__` 隐藏 Key，错误 `details` 只放状态码与上游消息，
  不打印请求头。
- **幂等与安全**：workspace slug 走 `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` 白名单，
  不做字符串拼接进 URL 之外的任何地方；上传大小与文本长度都有上限。
- **无状态**：服务不保存会话；多轮上下文由 AnythingLLM 的 `sessionId` 承载。
