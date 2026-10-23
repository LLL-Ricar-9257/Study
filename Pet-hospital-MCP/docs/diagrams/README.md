# Pet-hospital-MCP 架构图

本目录存放 Pet-hospital-MCP 的架构与调用链图示，`:8080` 的 Go 后端与 `:8765` 的 Python MCP 适配层是**两个独立进程**，
MCP 只通过 HTTP 调用后端，从不直接访问 `pet.db`。

| 文件 | 内容 | 说明 |
| --- | --- | --- |
| [`01-architecture.mmd`](01-architecture.mmd) / [`01-architecture.png`](01-architecture.png) | 系统架构图 | 浏览器、API 调用方、AI Agent 三条入口；Go 进程内部的分层；MCP 与后端、数据库的关系 |
| [`02-list-pets-sequence.mmd`](02-list-pets-sequence.mmd) / [`02-list-pets-sequence.png`](02-list-pets-sequence.png) | `list_pets` 调用时序图 | 一次 MCP 工具调用如何穿过 Python 适配层，最终变成 Go 的列表查询 |

## 图 1：系统架构

![系统架构图](01-architecture.png)

图中刻意保留三个事实：

1. MCP 不直接访问数据库，只通过 HTTP 调 Go API；
2. MCP 目前只有 `list_pets` 一个工具，不画出完整的增删改查；
3. 读走内存索引，写才追加日志，压实是虚线触发的维护动作。

## 图 2：`list_pets` 调用时序

![list_pets 调用时序图](02-list-pets-sequence.png)

这张图同时说明：无状态 HTTP 传输、REST 客户端职责、统一响应信封、工具层结构化输出。

## 重新生成

`.mmd` 是唯一需要维护的源文件，PNG 只是导出产物。

1. 打开 [mermaid.live](https://mermaid.live/)，左侧切到 **Code**，粘贴对应 `.mmd` 的内容；
2. 在 **Config** 中统一中文字体，避免导出后中文变方框：

   ```yaml
   theme: base
   themeVariables:
     fontFamily: "Microsoft YaHei, PingFang SC, sans-serif"
   ```

3. 右上角 **Actions** → **PNG**，按原文件名覆盖本目录中的图片。

> GitHub 的 Markdown 也能直接渲染 Mermaid，但本仓库统一引用 PNG，
> 保证在 VS Code、离线文档和 PPT 中显示一致。
