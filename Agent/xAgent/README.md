# xAgent

一个基于 **Textual** 的本地文件管理智能体。项目由原来的单文件
`practice02/chat_file_tools.py` 重构而来，核心目标是把 **业务核心（core）**
与 **终端界面（tui）** 彻底解耦，同时为后续开发 Web / Desktop 客户端预留稳定接口。

![xAgent TUI 预览（演示数据）](docs/tui-preview.png)

## 特性

- **Core / UI 分离**：`core` 不依赖 Textual、Rich，也不读写终端；`tui` 不直接操作文件或调用模型。
- **事件流驱动**：核心通过 `AgentEvent` 异步流输出文本、工具调用、完成和错误状态；
  Textual、WebSocket、桌面端可以消费同一套事件。
- **10 个文件工具 + 系统时间工具**：列目录、读取、创建、编辑、删除、重命名、创建目录、识别文件类型，
  以及获取当前日期、时间、时区和星期。
- **工作区沙箱**：所有路径统一经过 `Workspace.resolve()` 校验，禁止访问工作区之外的文件。
- **工具调用循环**：自动处理模型的多轮 function calling；工具错误回传给模型，不会中断界面。
- **流式 TUI**：Markdown 增量渲染、工具调用折叠卡片、状态栏、取消生成、清空会话。
- **opencode 风格输入区**：无边框 composer、`>` 提示符、琥珀色光标，下方显示快捷键与运行状态。
- **中文命令面板**：`Ctrl+P` 打开，命令、搜索提示和空结果提示全部为中文。
- **马卡龙配色**：消息与工具调用不使用整块背景，仅用薄荷绿、奶油黄、粉色和淡紫做文字强调。
- **终端宽度安全**：不使用 box-drawing 边框；Windows `cmd.exe` 启动时自动切换黑体，
  避免中英文混排造成边框/表格错位。
- **可配置限额**：工具轮数、文本读取上限、目录列出的最大条目数。
- **安全默认值**：API Key 不会进入 `repr`、日志或 UI 事件；`config.ini` 已被 Git 忽略。
- **无 UI 测试**：核心工具、Agent、配置和架构约束均可独立测试。

## 目录结构

```text
xAgent/
├─ core/                         # 业务核心：无终端、无 UI 依赖
│  ├─ __init__.py                # 对外公共 API
│  ├─ agent.py                   # 对话编排 + 工具循环
│  ├─ bootstrap.py               # 组合根：配置 -> Agent
│  ├─ config.py                  # config.ini / 环境变量 / 限额
│  ├─ errors.py                  # 领域异常
│  ├─ events.py                  # UI 无关的领域事件（显示协议）
│  ├─ models.py                  # 供应商无关的消息/分片结构
│  ├─ ports.py                   # ChatModel 抽象端口
│  ├─ prompts.py                 # 系统提示词
│  ├─ llm/
│  │  └─ openai_chat.py          # OpenAI 兼容流式适配器
│  └─ tools/
│     ├─ filesystem.py           # 10 个文件工具 + 时间工具的实现
│     ├─ paths.py                # 工作区路径策略与名称校验
│     ├─ registry.py             # 工具注册、JSON 参数校验、异常隔离
│     ├─ specs.py                # 工具的 JSON Schema 声明
│     └─ types.py                # ToolSpec / ToolResult
├─ tui/                          # Textual 表现层
│  ├─ app.py                     # 布局、输入、事件消费、生命周期
│  ├─ widgets.py                 # 消息/工具卡片组件
│  ├─ formatting.py              # 纯展示格式化 + 宽度安全字符处理
│  ├─ palette.py                 # 全中文命令面板
│  ├─ terminal.py                # Windows 控制台字体切换（黑体）
│  ├─ styles.tcss                # Textual 主题（无 box-drawing 边框）
│  ├─ cli.py                     # 命令行入口
│  └─ __main__.py                # python -m tui
├─ tests/                        # 核心、TUI 冒烟、架构约束测试
├─ docs/
│  ├─ architecture.md            # 分层设计与扩展指南
│  └─ tui-preview.png
├─ config.example.ini
├─ config.ini                    # 本机真实配置，已被 .gitignore 忽略
├─ pyproject.toml
└─ README.md
```

## 快速开始

要求 Python 3.11+。

```powershell
cd E:\work\LLM\Agent\xAgent

# 首次运行：创建虚拟环境并安装
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 配置 API Key
Copy-Item config.example.ini config.ini
# 编辑 config.ini，填入 [llm] api_key
```

启动：

```powershell
# 工作区默认为当前目录
.\.venv\Scripts\xagent.exe

# 或显式指定工作区 / 配置文件
.\.venv\Scripts\xagent.exe --workspace E:\work\my-project
.\.venv\Scripts\xagent.exe --config .\config.ini --workspace .\demo

# 也可以模块方式启动
.\.venv\Scripts\python.exe -m tui -w E:\work\my-project
```

安装后也可以直接使用 `xagent` 命令（前提是虚拟环境已激活）。

## 配置

`config.ini`（参考 `config.example.ini`）：

```ini
[llm]
api_key = sk-your-key-here
base_url = https://api.deepseek.com/v1
model = deepseek-chat
timeout_seconds = 120

[agent]
# 留空则使用启动时的当前目录；相对路径以配置文件目录为基准
workspace =
max_tool_rounds = 20
max_text_bytes = 5242880
default_max_chars = 50000
max_list_items = 2000
```

环境变量优先级高于配置文件：

| 环境变量 | 说明 |
| --- | --- |
| `XAGENT_API_KEY` | API Key（也兼容 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`） |
| `XAGENT_BASE_URL` | OpenAI 兼容接口地址 |
| `XAGENT_MODEL` | 模型名 |
| `XAGENT_WORKSPACE` | 默认工作区 |
| `XAGENT_CONFIG` | 配置文件路径 |
| `XAGENT_CONSOLE_FONT` | cmd.exe 自动切换的控制台字体，默认 `黑体` |
| `XAGENT_NO_FONT_SETUP` | 设为 `1` 时禁止自动修改控制台字体 |

优先级：命令行参数 > 环境变量 > `config.ini` > 默认值。API Key 只保存在核心配置对象中，
`LLMSettings` 的 `repr` 已隐藏该字段。

## TUI 操作

| 操作 | 说明 |
| --- | --- |
| `Enter` | 发送当前输入 |
| `Esc` | 取消正在生成的一轮 |
| `Ctrl+L` | 清空界面与对话历史 |
| `Ctrl+P` | 打开中文命令面板 |
| `Ctrl+Q` | 退出 |
| `/help` | 显示命令帮助 |
| `/clear` | 清空会话 |
| `/workspace` | 查看当前工作区 |
| `/cancel` | 取消当前生成 |
| `/quit` | 退出 |

取消或模型失败时，核心会回滚本轮对话历史；但已经执行完成的文件操作不会被自动撤销，
界面会展示工具执行结果。

### Windows 终端字体

在 `cmd.exe` 中启动时，xAgent 会尝试通过 Windows 控制台 API 将字体切换为
**黑体**，并保留原来的字号。这样可以避免默认字体缺少等宽中文字形导致的边框、
表格和 CJK 字符错位。设置失败时不会影响程序运行。

- 字体名可用 `XAGENT_CONSOLE_FONT` 覆盖，例如 `Consolas`。
- 设置 `XAGENT_NO_FONT_SETUP=1` 可禁用自动切换。
- **Windows Terminal** 的字体由终端自身设置管理，控制台 API 可能无效；建议在
  Windows Terminal 设置中选择 `Sarasa Mono SC`、`Microsoft YaHei Mono` 或
  `Consolas` 等等宽字体。
- TUI 不再使用 Unicode 制表符绘制边框，模型输出中的易错宽度标点也会在展示层
  转换为 ASCII，进一步保证对齐。

### 中文命令面板

按 `Ctrl+P` 打开命令面板，输入中文或拼音片段即可筛选。内置命令：

- 显示命令帮助
- 清空对话
- 取消当前生成
- 查看当前工作区
- 聚焦输入框
- 退出 xAgent

Textual 自带的英文系统命令（Theme、Quit、Keys 等）已全部移除，
搜索框提示和“没有匹配命令”的提示也已替换为中文。命令列表的选中态
和鼠标悬停态只改变文字颜色，不绘制底色块。

## 核心 API 与未来 UI

任何新界面都只需要依赖 `core`，典型用法：

```python
from dataclasses import asdict
from core import TextDelta, ToolFinished, ToolStarted, TurnCompleted, build_agent, load_config

config = load_config()
agent = build_agent(config)

async for event in agent.send("列出当前目录"):
    if isinstance(event, TextDelta):
        ...                       # 追加到一个文本节点
    elif isinstance(event, ToolStarted):
        ...                       # 新建工具调用卡片
    elif isinstance(event, ToolFinished):
        ...                       # 更新卡片状态
    elif isinstance(event, TurnCompleted):
        ...                       # 收尾
    else:
        payload = asdict(event) | {"type": type(event).__name__}
        ...                       # WebSocket 可以直接发送 JSON

await agent.aclose()
```

一个 `FileAgent` 实例代表一个独立会话，内部有消息历史和 `asyncio.Lock`。
Web 端可以为每个连接创建一个 Agent 实例；Desktop 端可以按窗口创建实例。
核心只暴露异步事件流，不假设 UI 是终端、网页还是桌面窗口。

详见 [docs/architecture.md](docs/architecture.md)。

## 分层与依赖规则

```text
tui (Textual)
   │  只依赖 core 公共 API
   ▼
core.agent ──► core.ports.ChatModel  ◄── core.llm.openai_chat (适配器)
   │
   └────────► core.tools.registry ◄──── core.tools.filesystem (适配器)
```

必须遵守的规则：

1. `core` 禁止 `import textual`、`import rich`、`from tui ...`。
2. `tui` 禁止直接读写文件、禁止直接创建 OpenAI 客户端。
3. UI 只消费 `AgentEvent`，不依赖 OpenAI SDK 的数据结构。
4. 新增模型供应商时，实现 `core.ports.ChatModel`，不修改 `FileAgent`。
5. 新增文件工具时，同时更新 `tools/specs.py` 与 `tools/filesystem.py`，由注册表绑定。

`tests/test_architecture.py` 会自动检查第 1 条约束。

## 运行测试与静态检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check core tui tests
```

当前：**26 个测试通过**，Ruff 无告警。

## 从单文件版迁移

| 原 `chat_file_tools.py` | 新位置 |
| --- | --- |
| `WORKSPACE` 全局常量 | `core.tools.paths.Workspace` 实例 |
| 10 个 `*_tool()` 函数 | `core.tools.filesystem.FileToolset` 方法 |
| `TOOLS` 声明 | `core.tools.specs.FILE_TOOL_SPECS` |
| `TOOL_FUNCTIONS` | `ToolRegistry` |
| `_load_client()` | `core.config.load_config` + `OpenAIChatModel` |
| `stream_chat()` / `run_tool_calls()` | `FileAgent.send()` + `AgentEvent` |
| `print()` / `input()` | Textual `tui.app.FileAgentApp` |
| `--self-test` | `tests/`（pytest） |
