# xAgent 架构说明

## 1. 目标

原实现把「文件工具 + OpenAI 调用 + 工具循环 + 控制台输入输出」放在同一个文件中。
本重构按关注点拆分为两个一级包：

- `core`：业务规则、用例编排、领域事件、端口与适配器。
- `tui`：Textual 表现层，只负责输入采集和事件渲染。

未来增加 `web`、`desktop` 时，它们应当像 `tui` 一样只依赖 `core`，
而不是复制 `core` 中的业务逻辑。

## 2. 依赖方向

```text
┌─────────────────────────────────────────────────────────┐
│ 表现层（可替换）                                          │
│  tui/                future: web/ desktop/              │
└──────────────────────────┬──────────────────────────────┘
                           │ core 公共 API + AgentEvent
┌──────────────────────────▼──────────────────────────────┐
│ 应用层                                                   │
│  core.agent.FileAgent      核心用例：一轮对话、工具循环    │
│  core.bootstrap.build_agent 组合根                        │
└───────────────┬──────────────────────────┬───────────────┘
                │                          │
┌───────────────▼──────────────┐  ┌────────▼───────────────┐
│ 端口（core.ports）           │  │ 工具边界                │
│ ChatModel.stream()           │  │ ToolRegistry / ToolSpec │
└───────────────▲──────────────┘  └────────▲───────────────┘
                │                          │
┌───────────────┴──────────────┐  ┌────────┴───────────────┐
│ 适配器                       │  │ 适配器                  │
│ llm/openai_chat.py           │  │ tools/filesystem.py     │
│ （唯一依赖 openai SDK 的地方）│  │ （唯一读写文件的地方）    │
└──────────────────────────────┘  └────────────────────────┘
```

依赖只允许从外层指向内层。`core.agent` 不认识 OpenAI SDK，也不认识 Textual。
`tui` 不直接调用 `FileToolset` 或 `OpenAI`。

## 3. 关键模块

### `core.agent.FileAgent`

一个实例代表一个会话，维护：

- `messages`：system / user / assistant / tool 消息历史；
- `asyncio.Lock`：同一会话串行处理；
- `max_tool_rounds`：防止模型在工具循环中失控。

`send(text)` 是**异步生成器**：

1. 接收用户输入；
2. 拉取模型流，产生 `TextDelta`；
3. 聚合工具调用增量；
4. 执行工具（`asyncio.to_thread`，避免阻塞事件循环）；
5. 把工具结果写回历史，进入下一轮；
6. 正常结束产生 `TurnCompleted`，异常产生 `TurnFailed`。

生成器天然提供背压。UI 每消费一个事件才拉取下一个；取消时直接关闭生成器，
核心在 `finally` 中回滚本轮消息。

### `core.events`

事件是 UI 的显示协议，均为不可变 dataclass：

| 事件 | 含义 | UI 典型处理 |
| --- | --- | --- |
| `TurnStarted` | 一轮开始 | 状态改为“思考中” |
| `TextDelta` | 正文增量 | 追加到流式 Markdown |
| `ToolStarted` | 开始调用工具 | 新建工具卡片 |
| `ToolFinished` | 工具结束（成功或失败） | 更新卡片，失败展开 |
| `TurnCompleted` | 正常结束 | 停止光标、恢复输入 |
| `TurnFailed` | 失败且历史已回滚 | 显示错误 |
| `ToolRoundLimitReached` | 达到工具轮数上限 | 显示提示 |

事件不包含 OpenAI 原始 chunk、文件句柄或 UI 组件。

### `core.tools`

- `paths.Workspace`：统一解析并校验路径，越界时抛出 `WorkspaceBoundaryError`。
- `filesystem.FileToolset`：10 个文件工具 + 系统时间工具的实现，不依赖全局变量，便于测试和多工作区。
- `specs.FILE_TOOL_SPECS`：工具 JSON Schema，与实现解耦。
- `registry.ToolRegistry`：校验 JSON 参数、绑定签名、隔离异常。
  工具失败被转换为 `ToolResult(ok=False)`，作为 tool 消息回传给模型，
  不会把异常抛到 UI。

### `core.llm.openai_chat.OpenAIChatModel`

唯一接触 `openai` SDK 的模块。它把 OpenAI 的流式 chunk 转成
`core.models.ChatChunk`，把核心消息序列化为 OpenAI 消息格式。
替换成其它供应商时，只需实现：

```python
class ChatModel(Protocol):
    def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatChunk]: ...
```

## 4. 为什么适合多前端

### Web

每个 WebSocket 连接创建一个 `FileAgent`，把事件序列化为 JSON：

```python
payload = asdict(event) | {"type": type(event).__name__}
await websocket.send_json(payload)
```

取消时关闭异步生成器即可触发核心回滚。

### Desktop

桌面进程内直接调用 `build_agent(config)`，将 `TextDelta` / `ToolStarted`
映射到原生控件。无需启动 TUI，也无需修改核心配置。

### CLI（非 Textual）

同样消费 `AgentEvent` 并打印文本即可，说明业务层没有绑定终端。

### TUI 显示宽度

终端中文字体常把 `—`、`…`、`“”`、`│` 等“东亚宽度不确定”字符渲染成双宽，
而 Textual 按单宽计算，导致边框和表格错位。TUI 层做了两件事：

1. `tui/styles.tcss` 不使用 Unicode 制表符边框，改用背景色和留白区分区域；
2. `tui/formatting.py` 在展示前把高风险字符替换为 ASCII，并且只影响 UI，
   不修改核心事件和模型上下文。

在 Windows `cmd.exe` 下，`tui/terminal.py` 还会尝试把控制台字体切换为黑体，
保留原字号；接口不可用或用户禁用时静默跳过。底部 composer 采用无边框、
背景分层的布局，输入框本身透明，避免整行色块和边框字符带来的视觉问题。
消息和工具调用也不使用整块背景，只通过薄荷绿、奶油黄、粉色和淡紫做
前景强调，避免终端把深蓝背景映射成亮蓝色块。

`tui/palette.py` 继承 Textual 的 `CommandPalette`，替换搜索提示和无匹配提示；
`FileAgentApp.get_system_commands()` 不再调用父类，而是只提供中文命令。

## 5. 扩展点

### 新增文件工具

1. 在 `core/tools/filesystem.py` 添加方法；
2. 在 `core/tools/specs.py` 添加 `ToolSpec`；
3. 在 `FileToolset.registry_handlers()` 注册方法名；
4. 在 `tests/` 增加对应测试。

`ToolRegistry` 会在构造时检查声明和实现是否一一对应。

### 新增模型供应商

1. 新建 `core/llm/<provider>.py`；
2. 实现 `ChatModel.stream()` 返回 `ChatChunk`；
3. 在 `core/bootstrap.py` 选择适配器（或增加工厂参数）；
4. 核心 Agent 无需修改。

### 新增界面

1. 新建 `web/` 或 `desktop/` 包；
2. 只从 `core` 导入 `load_config`、`build_agent`、事件类型；
3. 实现输入采集 + 事件渲染；
4. 不要 import `tui`，也不要在 UI 中直接操作文件。

## 6. 一致性与容错

- **模型错误**：清理本轮新增消息，避免半截 assistant/tool 历史污染下一轮。
- **用户取消**：关闭生成器，核心回滚未完成的一轮。
- **工具错误**：不终止对话，错误文本回传模型，由模型解释给用户。
- **工具轮数超限**：停止循环，写入一条明确的 assistant 说明。
- **文件写入**：使用临时文件 + `os.replace`，避免中途失败破坏原文件。
- **路径安全**：所有相对路径基于工作区；解析后必须仍位于工作区内；
  根目录不可删除或重命名。

注意：取消只回滚对话历史，不撤销已完成的文件系统副作用。这是文件工具本身的语义，
UI 会展示每个工具的执行结果。

## 7. 测试策略

- `tests/test_filesystem_tools.py`：文件工具与时间工具、路径越界、覆盖保护、注册表错误。
- `tests/test_agent.py`：工具循环、模型失败回滚、并发拒绝、轮数上限、关闭。
- `tests/test_config.py`：配置优先级、相对工作区、Key 隐藏、错误配置。
- `tests/test_architecture.py`：core 不得依赖 Textual/Rich，事件契约公开。
- `tests/test_tui.py`：Textual 启动、流式回复、取消回滚、字符宽度归一化和无边框样式约束。
