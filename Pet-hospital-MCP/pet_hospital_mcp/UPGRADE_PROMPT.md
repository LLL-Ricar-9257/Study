# UPGRADE_PROMPT · 从 SDK 1.x FastMCP 迁移到 SDK 2.x MCPServer

这份文档记录的是**本项目在 `mcp==2.0.0` 上实际验证过的 API**，
不是从 FastMCP 记忆里推出来的。迁移时照着这里改，别照着 1.x 的习惯写。

---

## 1. 入口类换名字了

```python
# ❌ 1.x
from mcp.server.fastmcp import FastMCP

# ✅ 2.x
from mcp.server import MCPServer

mcp = FastMCP("demo")        # 1.x
mcp = MCPServer("demo")      # 2.x
```

* `mcp==2.0.0` 里 **`mcp.server.fastmcp` 这个模块已经不存在**，
  `import` 它会直接 `ModuleNotFoundError`；
* `MCPServer` 支持 `name` / `title` / `description` / `instructions` /
  `version` / `icons` / `lifespan` / `extensions` 等；
* 本项目刻意**没有**实现、兼容或迁移 1.x 的 FastMCP 服务。

---

## 2. `streamable_http_app()` 的参数变少了

2.0.0 的签名：

```python
MCPServer.streamable_http_app(
    *,
    streamable_http_path="/mcp",
    json_response=False,
    stateless_http=False,
    event_store=None,
    retry_interval=None,
    max_request_body_size=4194304,
    transport_security=None,
    host="127.0.0.1",
)
```

* `session_idle_timeout` / `max_sessions` **不存在**（2.2.0 才加回来），
  也就是说 2.0.0 根本没有会话过期与会话上限这套东西；
* 无状态服务固定三件套：

```python
app = server.streamable_http_app(
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
    event_store=None,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    host="127.0.0.1",
)
```

> ⚠️ `host` 传 `127.0.0.1` / `localhost` / `::1` 时，SDK 会**自动开启**
> DNS rebinding 防护（校验 `Host` 与 `Origin`）。教学场景要关掉，就得像上面
> 这样显式传 `TransportSecuritySettings`。

---

## 3. 自定义路由用 `custom_route`

```python
from starlette.requests import Request
from starlette.responses import JSONResponse

@server.custom_route("/health", methods=["GET"], name="health")
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy"})
```

* 处理器签名固定 `(request: Request) -> Response`，且**必须 async**；
* 这些路由不进授权中间件，也不出现在 MCP 协议面里，正适合放健康检查；
* `custom_route` 注册的路由会挂到 `streamable_http_app()` 产出的同一个
  Starlette 应用上。

---

## 4. 工具注册：`add_tool` / `@tool`

```python
from mcp_types import ToolAnnotations

@server.tool(
    name="list_pets",                                  # snake_case
    title="查询宠物档案列表",
    description=LIST_PETS_DESCRIPTION,
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False,
        idempotent_hint=True, open_world_hint=False,
    ),
)
async def list_pets(species: str | None = None) -> PetListData:
    ...
```

`add_tool(fn, name=..., title=..., description=..., annotations=...,
icons=..., meta=..., structured_output=...)` 是等价的非装饰器写法。

### 坑 1：单个 `BaseModel` 参数**不会**被展开

1.x 的 FastMCP 里 `def tool(params: MyModel)` 会把模型字段摊平成
`inputSchema`。**2.x 不这么做了**：

```python
async def tool(params: MyModel): ...
# 2.0.0 生成的 schema 是：
# {"properties": {"params": {"$ref": "#/$defs/MyModel"}}, "required": ["params"]}
```

想让 Agent 看到扁平参数，就写**显式的多个形参**，
用 `Annotated[T | None, Field(description=...)]` 挂描述，用 `Literal[...]`
表达枚举，用 `Field(ge=/le=)` 表达范围。

### 坑 2：未知参数被**静默丢弃**

2.x 从签名合成的 `arg_model` 不带 `extra="forbid"`，多传的参数不会报错，
直接消失。要拒绝未知字段，需要自己收紧：

```python
tool = server._tool_manager.get_tool(tool_name)      # SDK 未提供公开入口
strict = create_model(original.__name__, __base__=(Mixin, original),
                      __config__=ConfigDict(extra="forbid"))
tool.fn_metadata.arg_model = strict
tool.parameters = strict.model_json_schema(by_alias=True)
```

收紧后 `inputSchema` 会出现 `additionalProperties: false`。
本项目把这段逻辑收在 `tools/_schema.py::harden_tool_arguments()`，
新增工具直接调用即可。

### 坑 3：Pydantic 报错原文会漏给客户端

`Tool.run()` 会把 `ValidationError` 的 `str()` 直接塞进 `ToolError`，
里面含 `errors.pydantic.dev` 链接和调用方的原始取值。本项目用一个
`model_validator(mode="wrap")` 把失败"存起来、原样放行"，
再由工具体抛出自己的统一错误结构，避免泄露。
细节见 `tools/_schema.py`。

---

## 5. 工具失败：用 `ToolError`，别手搓 `CallToolResult`

```python
from mcp.server.mcpserver.exceptions import ToolError
```

* 抛 `ToolError` → 结果 `is_error=True`，`content[0].text` 是你的消息，
  服务端按 **INFO** 记录、**不打 traceback**；
* 抛别的异常 → 客户端只看到 `Error executing tool <name>`，
  服务端按 **ERROR** 记录完整 traceback；
* 想要**结构化输出**就正常 `return` 一个 `BaseModel`，
  SDK 会自动校验、生成 `outputSchema`、填 `structuredContent`。
  `CallToolResult` 仍然可以 `return`，但那样就不是结构化路径了。

本项目所有失败路径统一是：`raise ToolError(<统一错误 JSON>)`，
`ToolError` 会被 SDK 加上 `Error executing tool list_pets: ` 前缀，
前缀之后就是可 `json.loads` 的完整错误信封。

---

## 6. 生命周期交给 `lifespan`

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(_: MCPServer) -> AsyncIterator[None]:
    client = PetHospitalRestClient(settings)
    try:
        yield None
    finally:
        await client.aclose()

server = MCPServer(name="demo", lifespan=lifespan)
```

`streamable_http_app()` 产出的 Starlette 应用把 `lifespan` 接到
session manager 上。**测试里要自己驱动**它：

```python
async with app.router.lifespan_context(app):
    ...
```

> 别把 lifespan 放进 pytest 的 `async` fixture：setup 和 teardown 跑在
> 不同 task，anyio 的 task group 退出时会报
> `Attempted to exit cancel scope in a different task than it was entered in`。
> 在测试函数体里 `async with` 才安全。

---

## 7. 2026-07-28 的无状态调用长什么样

这是与 1.x 差别最大的地方。

| | 1.x（有状态） | 2.x 无状态（2026-07-28） |
| --- | --- | --- |
| 握手 | 必须 `initialize` + `notifications/initialized` | **不需要** `initialize` |
| 会话 | 服务端下发 `Mcp-Session-Id`，后续请求都要带 | **不下发、不接受** |
| 协议版本声明 | 靠握手协商 | 每个请求带 `MCP-Protocol-Version` 头 + `params._meta` |
| 方法路由 | 无 | `Mcp-Method` 头必须等于 body 的 `method` |
| 具名方法 | 无 | `Mcp-Name` 头必须等于 body 的 `params.name`（`tools/call` / `prompts/get` / `resources/read`） |

一个自包含的 `tools/call`：

```http
POST /mcp
content-type: application/json
accept: application/json, text/event-stream
mcp-protocol-version: 2026-07-28
mcp-method: tools/call
mcp-name: list_pets

{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "list_pets",
    "arguments": {"species": "犬"},
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientCapabilities": {}
    }
  }
}
```

校验阶梯（首个失败为准）：

1. `params._meta` 必须是对象，且带
   `io.modelcontextprotocol/protocolVersion` 与
   `io.modelcontextprotocol/clientCapabilities`；
2. `MCP-Protocol-Version` 头 == 信封里的版本，`Mcp-Method` == body 的 method，
   `Mcp-Name` == body 的具名参数；
3. 版本在 `MODERN_PROTOCOL_VERSIONS`（2.0.0 里就是 `("2026-07-28",)`）之内。

违反前两条 → HTTP **400** + JSON-RPC `error`；
违反第三条 → HTTP 400 + `code = -32022`（`UNSUPPORTED_PROTOCOL_VERSION`）。

常量都从 SDK 取，不要硬编码字符串：

```python
from mcp.shared.inbound import (
    PROTOCOL_VERSION_META_KEY,      # io.modelcontextprotocol/protocolVersion
    CLIENT_CAPABILITIES_META_KEY,  # io.modelcontextprotocol/clientCapabilities
    CLIENT_INFO_META_KEY,
    MCP_PROTOCOL_VERSION_HEADER,   # mcp-protocol-version
    MCP_METHOD_HEADER,             # mcp-method
    MCP_NAME_HEADER,               # mcp-name
    MODERN_PROTOCOL_VERSIONS,
)
```

无状态下 `GET /mcp` 返回 **405**（没有独立的 GET 流），`DELETE /mcp` 同样 405。

---

## 8. 其它 API 差异备忘

| 1.x | 2.0.0 |
| --- | --- |
| `mcp.server.fastmcp.FastMCP` | `mcp.server.MCPServer` |
| `from mcp.types import CallToolResult` | `from mcp_types import ...`（类型拆包） |
| `FastMCP.settings.port` | 传给 `streamable_http_app()` / uvicorn |
| `mcp.run(transport="streamable-http", port=…)` | 同名可用；`session_idle_timeout` / `max_sessions` 不可用 |
| `Context` | `mcp.server.mcpserver.context.Context`（不变） |
| `ToolError` | `mcp.server.mcpserver.exceptions.ToolError`（`is_error=True` 语义不变） |

`FastMCP` 的一个兼容层（`mcp.server.fastmcp`）在 2.0.0 已被移除，
本项目按要求不做任何兼容层。

---

## 9. 一份最小可运行骨架

```python
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.requests import Request
from starlette.responses import JSONResponse


@asynccontextmanager
async def lifespan(_: MCPServer) -> AsyncIterator[None]:
    yield None


server = MCPServer(name="demo", version="0.1.0", lifespan=lifespan)


@server.tool(name="echo", description="原样返回输入")
async def echo(text: str) -> str:
    if not text:
        raise ToolError('{"error":{"code":"VALIDATION_ERROR","message":"text 不能为空","details":{}}}')
    return text


@server.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy", "protocolVersion": LATEST_PROTOCOL_VERSION})


app = server.streamable_http_app(
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
    event_store=None,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    host="127.0.0.1",
)

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="127.0.0.1", port=8765)
```