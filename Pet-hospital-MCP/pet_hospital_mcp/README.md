# 宠物医院 MCP 服务

把已有的 **Go 宠物医院 REST API** 包装成 AI Agent 可调用的 **MCP 工具**。
独立 Python 进程，只通过 HTTP 调用后端，不修改后端一行代码。

> ## 目录关系先搞清楚
>
> 仓库里有**两个同级的目录**，名字只差一个字符，千万别进错：
>
> ```text
> Pet-hospital-MCP\
> ├── pet-hospital-mcp\     ← 连字符：Go 宠物医院 REST API（后端）
> └── pet_hospital_mcp\     ← 下划线：Python MCP 服务（本项目）
> ```
>
> 本 README 全部命令都在 **`pet_hospital_mcp\`**（下划线）里执行。

| 项 | 值 |
| --- | --- |
| MCP Python SDK | **`mcp==2.0.0`** |
| MCP 协议版本 | **`2026-07-28`**（`mcp.types.LATEST_PROTOCOL_VERSION`） |
| 服务端实现 | **`mcp.server.MCPServer`**（SDK 2.x；不使用 `mcp.server.fastmcp`） |
| 传输 | **无状态 Streamable HTTP**（`stateless_http=True`, `json_response=True`） |
| MCP 端点 | `POST http://127.0.0.1:8765/mcp` |
| 健康检查 | `GET http://127.0.0.1:8765/health` |
| 阶段一工具 | 只有 `list_pets` |

> **无状态意味着什么**
>
> * 不发送 `initialize`；
> * 不返回、也不接受 `Mcp-Session-Id`；
> * 没有会话存储、没有会话过期、没有 `max_sessions`、没有有状态 SSE 断点续传；
> * 每个 POST 都是一次自包含的请求—响应，服务重启不丢任何东西。

---

## 1. 先启动 Go REST API

MCP 服务只是适配层，**必须先有后端在跑**。需要**两个终端窗口**。

### 1.1 有 Go（1.22+）

```bat
cd /d E:\work\LLM\Pet-hospital-MCP\pet-hospital-mcp
go run . -seed -count 2000
```

### 1.2 没装 Go（用预编译发行包）

仓库里已经放好了编译好的 exe，直接跑：

```bat
cd /d E:\work\LLM\Pet-hospital-MCP\pet-hospital-mcp\dist\windows
pethospital.exe
```

> PowerShell 里同理，把 `cd` 换成 `Set-Location`，
> 激活脚本用 `.\.venv\Scripts\Activate.ps1`（cmd 里要用 `activate.bat`，但见下方说明——其实不激活更省事）。

### 确认后端活着

```bat
curl -s http://127.0.0.1:8080/health
```

```json
{"code":200,"message":"ok","data":{"petCount":1008,"status":"healthy",
 "uptime":"1m6s","dbFile":"data\\pet.db",
 "timestamp":"2026-10-10T09:28:34+08:00"},"time":"..."}
```

端口被占用就换一个，并同步告诉 MCP 服务：

```bat
pethospital.exe -addr 127.0.0.1:18080
:: 另一个窗口
set PET_HOSPITAL_BASE_URL=http://127.0.0.1:18080
```

---

## 2. 安装 MCP 服务（一次性）

要求 **Python 3.11+**。在 `pet_hospital_mcp\` 目录里：

```bat
cd /d E:\work\LLM\Pet-hospital-MCP\pet_hospital_mcp

python -m venv .venv

:: 不需要激活虚拟环境，直接用里面的解释器即可（省掉 cmd/PowerShell 激活差异）
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

> 国内镜像若没同步 `mcp` / `setuptools`，加官方源：
> `.venv\Scripts\python.exe -m pip install -i https://pypi.org/simple -e ".[dev]"`
>
> 想激活也行：cmd 用 `.venv\Scripts\activate.bat`，PowerShell 用 `.\.venv\Scripts\Activate.ps1`。
> 激活后 `python` / `pytest` 就直接指向 venv。
> **注意**：PowerShell 里裸写 `pytest` 有时会命中别处的可执行文件，
> 最保险的写法永远是 `.venv\Scripts\python.exe -m pytest -q`。

---

## 3. 启动 MCP 服务（第二个终端窗口）

```bat
cd /d E:\work\LLM\Pet-hospital-MCP\pet_hospital_mcp
.venv\Scripts\python.exe -m pet_hospital_mcp
```

启动后会打印一行 JSON 日志：

```json
{"timestamp":"2026-10-10T09:29:32+0800","level":"INFO","logger":"pet_hospital_mcp.server",
 "message":"mcp_endpoint","url":"http://127.0.0.1:8765/mcp",
 "health":"http://127.0.0.1:8765/health"}
```

### 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PET_HOSPITAL_BASE_URL` | `http://127.0.0.1:8080` | Go REST API 根地址 |
| `MCP_HOST` | `127.0.0.1` | MCP 服务监听地址（默认只听本机） |
| `MCP_PORT` | `8765` | MCP 服务监听端口 |
| `MCP_PATH` | `/mcp` | MCP 端点路径 |
| `MCP_REQUEST_TIMEOUT` | `10.0` | 单次上游请求超时（秒） |
| `MCP_MAX_RETRIES` | `2` | 上游失败后的额外重试次数 |
| `MCP_RETRY_BACKOFF` | `0.2` | 首次退避秒数，之后按倍数递增 |
| `MCP_LOG_LEVEL` | `INFO` | 日志级别 |

```bat
:: cmd：同一条命令里设置
set PET_HOSPITAL_BASE_URL=http://192.168.1.10:8080
set MCP_PORT=9000
.venv\Scripts\python.exe -m pet_hospital_mcp
```

```powershell
# PowerShell
$env:PET_HOSPITAL_BASE_URL="http://192.168.1.10:8080"
$env:MCP_PORT="9000"
.\.venv\Scripts\python.exe -m pet_hospital_mcp
```

---

## 4. 验证

### 4.1 `/health`

```bash
curl -s http://127.0.0.1:8765/health
```

```json
{"status":"healthy","server":"pet-hospital-mcp","version":"0.1.0",
 "protocolVersion":"2026-07-28","sdk":"mcp==2.0.0",
 "transport":"streamable-http (stateless)","backend":"http://127.0.0.1:8080"}
```

`/health` 只报告 MCP 服务自身，**不会**去打后端——后端停掉时 MCP 仍算健康。

### 4.2 裸 HTTP 调用（2026-07-28 无状态流程）

这是最能说明"新协议长什么样"的方式：**没有 `initialize`，没有 session id**。

发现工具：

```bash
curl -s http://127.0.0.1:8765/mcp \
  -X POST \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -H 'mcp-protocol-version: 2026-07-28' \
  -H 'mcp-method: tools/list' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {
      "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {}
      }
    }
  }'
```

调用 `list_pets`：

```bash
curl -s http://127.0.0.1:8765/mcp \
  -X POST \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -H 'mcp-protocol-version: 2026-07-28' \
  -H 'mcp-method: tools/call' \
  -H 'mcp-name: list_pets' \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "list_pets",
      "arguments": {
        "species": "犬",
        "status": "已康复",
        "sortBy": "totalCost",
        "order": "desc",
        "page": 1,
        "pageSize": 2
      },
      "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {}
      }
    }
  }'
```

响应（节选）：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "isError": false,
    "resultType": "complete",
    "structuredContent": {
      "items": [
        {"id": "PET-000649", "name": "椰子", "species": "犬", "status": "已康复",
         "doctor": "郑医生", "totalCost": 23338.75, "visitCount": 4, "records": [], "charges": []}
      ],
      "total": 260, "page": 1, "pageSize": 2, "totalPages": 130, "totalCost": 985328.26
    },
    "content": [{"type": "text", "text": "{ ...与 structuredContent 相同的 JSON... }"}]
  }
}
```

响应头里**没有** `mcp-session-id`。

### 4.3 官方 SDK 2.x 客户端

仓库里带了现成的验证脚本（**端口不硬编码**，读同一套环境变量）：

```bat
cd /d E:\work\LLM\Pet-hospital-MCP\pet_hospital_mcp
.venv\Scripts\python.exe scripts\verify_client.py
```

实际输出：

```text
→ 连接 http://127.0.0.1:8765/mcp
  （不发送 initialize，不携带 Mcp-Session-Id）

[1] 发现工具        : ['list_pets']
    协议版本        : 2026-07-28
    服务端          : pet-hospital-mcp 0.1.0
    标题 / 只读     : 查询宠物档案列表 / True
    入参字段        : disease, doctor, max, min, name, order, ownerName,
                      ownerPhone, page, pageSize, q, sortBy, species, status
    拒绝额外字段    : True
    出参字段        : items, total, page, pageSize, totalPages, totalCost

[2] 调用 list_pets  : isError=False
    total=446 page=1 pageSize=3 totalPages=149 totalCost=1741571.59
      - PET-000596 南瓜 / 犬 / 慢性病随访 / 陈医生 / cost=34910.43 / visits=4 / records=4 charges=18
      - PET-000216 大金 / 犬 / 就诊中   / 韩医生 / cost=32138.45 / visits=4 / records=4 charges=19
      - PET-000649 椰子 / 犬 / 已康复   / 郑医生 / cost=23338.75 / visits=4 / records=4 charges=19

[3] 错误路径        :
    ✓ 非法种类         isError=True code=VALIDATION_ERROR
    ✓ pageSize 超界   isError=True code=VALIDATION_ERROR
    ✓ min > max       isError=True code=VALIDATION_ERROR
    ✓ 未知字段         isError=True code=VALIDATION_ERROR

全部通过。
```

自己写客户端的话：

```python
import anyio
from mcp import Client

async def main():
    # Client(url) 会先探测 server/discover，再决定握手方式
    async with Client("http://127.0.0.1:8765/mcp") as client:
        listing = await client.list_tools()
        print([t.name for t in listing.tools])          # ['list_pets']

        result = await client.call_tool(
            "list_pets",
            {"species": "犬", "sortBy": "totalCost", "order": "desc", "pageSize": 2},
        )
        print(result.is_error)                          # False
        print(result.structured_content["total"])       # 过滤后的总条数
        print(result.structured_content["totalCost"])   # 注意是 camelCase，与后端一致

anyio.run(main)
```

### 4.4 MCP Inspector

Inspector 需要 CLI 依赖（`typer`）：

```bash
python -m pip install "mcp[cli]"
npx @modelcontextprotocol/inspector
```

在 Inspector 的 UI 里：

* **Transport Type** 选 `Streamable HTTP`
* **URL** 填 `http://127.0.0.1:8765/mcp`
* **Headers** 里加上（2026-07-28 必需的路由头）
  `mcp-protocol-version: 2026-07-28`（Inspector 会自行补齐其余头）
* 点 **Connect** → **List Tools** 应看到 `list_pets` → 点 **Run** 调用

> 老版本 Inspector 默认会发 `initialize`。本服务是无状态的，
> `initialize` 是可选的，发了也不影响 `tools/list` / `tools/call`。

---

## 5. `list_pets`

对应后端 `GET /api/v1/pets`，只支持这 14 个查询参数，与后端一一对应，
**没有**任何适配器私有参数：

```text
q  name  ownerName  ownerPhone  species  doctor  disease  status
min  max  sortBy  order  page  pageSize
```

### 入参校验

| 参数 | 约束 |
| --- | --- |
| `species` | `犬` / `猫` / `兔` / `鸟` / `仓鼠` / `爬宠` / `其他` |
| `status` | `待就诊` / `就诊中` / `住院中` / `已康复` / `慢性病随访` |
| `sortBy` | `id` `name` `ownerName` `species` `doctor` `disease` `status` `totalCost` `visitCount` `createdAt` `updatedAt` |
| `order` | `asc` / `desc` |
| `page` | `>= 1` |
| `pageSize` | `1 <= x <= 500` |
| `min` / `max` | `>= 0`，且 `min <= max` |

此外一律**拒绝**：未知字段、布尔值当数字、`NaN` / `Infinity`、类型不正确的取值。
所有约束都写进了 `tools/list` 公布的 `inputSchema`（含 `additionalProperties: false`）。

### 返回值

对应 Go 响应信封里的 `data`：

| 字段 | 说明 |
| --- | --- |
| `items` | 当前页的宠物档案数组 |
| `total` | 过滤后的总条数 |
| `page` / `pageSize` / `totalPages` | 分页位置 |
| `totalCost` | 结果集花费合计（元） |

每条 `items` 里 `records` / `charges` **可能是 `null`**（Go 的空切片序列化成 `null`），
也可能非 `null`；两种情况都原样保留，不会被改成 `[]`。

### 错误

所有失败（入参非法、后端超时、后端挂了、后端返回脏数据）都返回同一个结构：

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "工具入参校验失败",
    "details": {"errors": [{"field": "species", "reason": "literal_error"}]}
  }
}
```

| `code` | 含义 |
| --- | --- |
| `VALIDATION_ERROR` | 工具入参不合法（**不会**调用后端） |
| `BACKEND_TIMEOUT` | 调用后端超时（已重试仍失败） |
| `BACKEND_UNAVAILABLE` | 连不上后端（服务没起 / 连接被拒 / 传输层错误） |
| `BACKEND_API_ERROR` | 后端返回 4xx / 5xx 或非 200 的业务码 |
| `BACKEND_INVALID_RESPONSE` | 后端返回的不是合法 JSON，或不符合数据契约 |
| `INTERNAL_ERROR` | 兜底：任何未归类的异常 |

失败时 MCP 结果的 `isError` 为 `true`，`content[0].text` 就是上面这段 JSON
（前面带 SDK 自动加的 `Error executing tool list_pets: ` 前缀）。
HTTPX / Pydantic / SDK / Python 堆栈**不会**出现在客户端可见的文本里。

### 日志

每次工具调用一行 JSON，字段固定：

```json
{"timestamp":"2026-01-01T00:00:00+0800","level":"INFO",
 "logger":"pet_hospital_mcp.tools.list_pets","message":"tool_call",
 "tool_name":"list_pets","params":{"ownerPhone":"***","species":"犬"},
 "status":"success","duration_ms":12.418,"error_code":null}
```

`ownerPhone`、`ownerAddr`、`chipNo`（含 `snake_case` 写法）在日志里**递归脱敏**，
其余字段照常记录。

---

## 6. 单元测试

**不需要启动 Go 服务，也不需要启动 MCP 服务**，测试全部走 `httpx.MockTransport`。

```bat
cd /d E:\work\LLM\Pet-hospital-MCP\pet_hospital_mcp
.venv\Scripts\python.exe -m pytest -q
```

预期：

```text
........................................................................ [ 37%]
........................................................................ [ 75%]
..............................................                           [100%]
190 passed in 1.81s
```

```bash
pytest -q tests/test_stateless_http.py      # 只跑无状态 HTTP 流程
pytest -q tests/test_list_pets_tool.py     # 只跑工具层
```

---

## 7. 目录结构

```text
pet_hospital_mcp/
├── pyproject.toml
├── README.md
├── UPGRADE_PROMPT.md
├── scripts/
│   └── verify_client.py     # 用官方 SDK 2.x 客户端做端到端验证
├── src/
│   └── pet_hospital_mcp/
│       ├── __init__.py
│       ├── __main__.py          # python -m pet_hospital_mcp
│       ├── config.py            # 环境变量 -> Settings
│       ├── server.py            # MCPServer + 无状态 Streamable HTTP + /health
│       ├── rest_client.py       # Go REST API 客户端（超时 / 重试 / 错误归类）
│       ├── errors.py            # 统一错误信封与 Pydantic 错误输出模型
│       ├── logging_config.py    # JSON 日志 + 递归脱敏
│       └── tools/
│           ├── __init__.py      # register_tools()：新增工具挂在这里
│           ├── _schema.py       # 入参收紧（拒绝未知字段）+ 统一校验错误
│           └── list_pets.py     # 阶段一工具
└── tests/
    ├── conftest.py
    ├── test_config_and_errors.py
    ├── test_list_pets_tool.py
    ├── test_logging_config.py
    ├── test_rest_client.py
    ├── test_stateless_http.py
    └── test_tool_registration.py
```

### 加下一个工具要做什么

1. 在 `src/pet_hospital_mcp/tools/` 新建模块，定义输入 / 成功输出模型；
2. 写 `build_xxx_tool(client)` 工厂返回绑定 REST 客户端的协程；
3. 在 `tools/__init__.py` 的 `register_tools()` 里调 `server.add_tool(...)`，
   并调用 `harden_tool_arguments(server, "xxx")`；
4. 在 `rest_client.py` 里加一个对应的方法，复用超时 / 重试 / 错误归类；
5. 照着 `list_pets` 抄测试。

REST 客户端、日志脱敏、统一错误约定全部复用，无需重写。

---

## 8. 教学场景的取舍

本服务**刻意不做**：认证、权限、CORS / Origin 校验。
因此显式关闭了 SDK 在本机监听时默认开启的 DNS rebinding 防护：

```python
TransportSecuritySettings(enable_dns_rebinding_protection=False)
```

这意味着它只适合跑在**本机 / 内网**，不要直接暴露到公网。