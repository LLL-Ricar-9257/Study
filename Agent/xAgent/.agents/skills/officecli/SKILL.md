---
name: officecli
description: 使用本地 officecli 命令行工具创建、读取、修改、查询和校验 Office 文档（.docx / .xlsx / .pptx），可导出文本、HTML、截图与 PDF。当用户要求生成或编辑 Word 文档、Excel 表格、PPT 演示文稿，或读取/分析这些文件的内容、样式、结构时使用。
---

# OfficeCLI 文档操作技能（docx / xlsx / pptx）

officecli 是单文件 CLI：无需安装 Office，不用 python-docx / openpyxl。本项目已内置二进制，直接用 Runcil 调用即可。

## 0. 执行环境（每次调用前必读）

- 二进制位置：`<xAgent 根目录>/office/tools/officecli/officecli.exe`（v1.0.158）。
  `office/` 已被 `.gitignore` 排除，**新克隆的仓库里没有这个二进制**；使用前请先从
  官方发布渠道下载对应平台的单文件可执行程序，改名为 `officecli.exe` 放到上述位置。
  缺失时 `scripts/officecli.cmd` 会以非零退出码失败，不会静默成功。
- **沙箱陷阱**：本环境对系统临时目录不可写，直接调用 exe 读文档会失败并报
  `Access to the path 'C:\Users\...\Temp\tmpXXXX.tmp' is denied.`
  必须通过包装脚本调用，它会把 `TEMP`/`TMP` 指向工作区 `.tmp_cli/`，并设置
  `OFFICECLI_NO_AUTO_RESIDENT=1`（每次 Runcil 都是新进程，resident 无意义且会残留锁文件）。

**标准调用方式（Runcil：程序名与参数分开）**

```
command: <xAgent 根目录>\.agents\skills\officecli\scripts\officecli.cmd
args: ["view", "report.docx", "outline"]
```

- 相对路径基于工作区根目录；建议一律使用相对路径，便于迁移。
- 含 `[N]` 的路径整体作为**一个参数**传入即可，Runcil 不经过 shell，无需引号转义。
- 不要使用管道、重定向、`$VAR`、`&&`；需要批量操作用 `batch` 命令（`--commands` 传 JSON 或 `--input file.json`）。
- 退出码：`0` 成功；`1` 失败（stderr 或 JSON 的 `error`）；`2` 已执行但有告警（读 `success` 字段判断）。
- 如果包装脚本缺失，可临时用 `cmd /c set TEMP=<工作区>\.tmp_cli & set TMP=<工作区>\.tmp_cli & <exe> ...` 兜底。

## 1. 心智模型：三层能力，优先用高层

**L1 读取 → L2 DOM 编辑 → L3 原始 XML**。能用 L1 解决就不改文件；L2 能表达就不碰 XML。

| 层 | 命令 | 用途 |
|---|---|---|
| L1 | `create` `view` `get` `query` `validate` `dump` `help` | 创建、只读探查、结构导出 |
| L2 | `set` `add` `remove` `move` `swap` `batch` | 语义化增删改 |
| L3 | `raw` `raw-set` `add-part` | L2 无法表达时的兜底（XML 直接改） |

## 2. 命令速查（args 逐项传入）

```text
create <file>                               # 按扩展名创建空白文档
view <file> outline|stats|issues|text|annotated|html|screenshot|pdf|forms
get <file> <path> --depth N [--json]         # 读取节点及子节点
query <file> <selector> [--json]             # CSS 风格查询
set <file> <path> --prop k=v [--prop ...]    # 修改属性/文本/格式
add <file> <parent> --type <type> --prop ... # 新增元素（--after/--before 定位）
remove <file> <path>                         # 删除元素
move <file> <path> --to <parent> --index N   # 移动
swap <file> <path1> <path2>                  # 交换
batch <file> --commands '<json array>' [--json] [--best-effort]
dump <file> [<path>]                         # 导出可回放的 batch 脚本
validate <file> [--json]                     # OpenXML schema 校验
raw <file> <part>；raw-set <file> <part> --xpath "..." --action replace --xml '<w:p>...'
help <format> [verb] [element] [--json]      # 权威 schema，禁止靠猜属性名
```

示例（docx）：

```text
["create", "out/report.docx"]
["add", "out/report.docx", "/body", "--type", "paragraph", "--prop", "text=2026 年度总结", "--prop", "style=Heading1", "--prop", "size=20pt", "--prop", "bold=true"]
["add", "out/report.docx", "/body", "--type", "paragraph", "--prop", "text=营收同比增长 18%。", "--prop", "size=11pt", "--prop", "lineSpacing=1.5x"]
["validate", "out/report.docx", "--json"]
["view", "out/report.docx", "outline"]
```

示例（xlsx）：

```text
["set", "out/data.xlsx", "/Sheet1/A1", "--prop", "value=姓名", "--prop", "bold=true"]
["set", "out/data.xlsx", "/Sheet1/B2", "--prop", "value=Alice"]
["add", "out/data.xlsx", "/Sheet1", "--type", "chart", "--prop", "type=bar", "--prop", "source=Sheet1!A1:B10"]
```

示例（pptx）：

```text
["create", "out/deck.pptx"]
["add", "out/deck.pptx", "/", "--type", "slide", "--prop", "title=Q4 汇报"]
["add", "out/deck.pptx", "/slide[1]", "--type", "shape", "--prop", "text=营收 +25%", "--prop", "x=2cm", "--prop", "y=5cm", "--prop", "size=24", "--prop", "color=FFFFFF"]
```

## 3. 值格式（`--prop` 的值）

| 类型 | 写法 | 示例 |
|---|---|---|
| 颜色 | 十六进制（可带 `#`）、命名色、rgb、主题色 | `FF0000`、`#FF0000`、`red`、`rgb(255,0,0)`、`accent1` |
| 间距 | 带单位 | `12pt`、`0.5cm`、`1.5x`、`150%` |
| 尺寸 | EMU 或带单位 | `914400`、`2.54cm`、`1in`、`72pt`、`96px` |
| 文本 | `text=` 中 `\n` 另起段落、`\v` 软换行、`\t` 制表符 | `--prop text=第一行\v第二行` |

`font.<attr>` 点号别名在 shape/run/paragraph/table/row/cell/section/styles 上通用：
`--prop font.color=red --prop font.bold=true --prop font.size=14pt`。

## 4. 路径约定

- 路径 **1-based**（XPath 风格）：`/body/p[3]` 是第 3 段；`--index` 是 **0-based**。
- **例外**：xlsx 的 `add --type row|col` 的 `--index` 是 1-based（与 Excel 行号/列序一致）。
- 稳定 ID：多步编辑优先用 `@id=` / `@paraId=` / `@commentId=` 形式，位置索引在插入删除后会漂移。
  - `/slide[1]/shape[@id=550950021]`、`/body/p[@paraId=1A2B3C4D]`
  - PPT 支持 `@name=`：`shape[@name=Title 1]`，但 `set`/`add` 用于 connector 的 from/to 时必须写全路径。
- 常用入口：docx `/`、`/body`、`/header[1]`、`/footer[1]`、`/styles`；xlsx `/Sheet1`、`/Sheet1/A1`；pptx `/`、`/slide[N]`。

## 5. 帮助优先（省时关键）

属性名、枚举值、别名不确定时，先查 help，不要试错：

```text
["help", "docx"]                       # 列出全部元素
["help", "docx", "paragraph"]          # 元素完整 schema（属性、别名、示例）
["help", "docx", "set", "paragraph"]   # 只列可用 set 的属性
["help", "xlsx", "pivottable", "--json"]
```

格式别名：`word`→`docx`、`excel`→`xlsx`、`ppt`/`powerpoint`→`pptx`。
help 与本文档冲突时，**以 help 为准**（版本更新）。

## 6. 内置专项技能（正式交付物必用）

二进制内置写作规范技能，输出前先加载并遵循（一个文档只加载一个最匹配的）：

```text
["load_skill", "word"]      # 报告/信函/备忘录通用
["load_skill", "pptx"]      # 通用演示：董事会、销售、全员会
["load_skill", "excel"]     # 通用表格、公式、透视表
["load_skill", "pitch-deck"]# 融资路演
["load_skill", "academic-paper"] / ["financial-model"] / ["data-dashboard"] / ["morph-ppt"]
```

返回的是 SKILL.md 全文，含字号层级、页眉页脚、QA 清单等硬性交付标准。
`references/` 下存有 word / pptx / excel 三份离线副本（版本可能落后，优先用 load_skill 实时拉取）。

## 7. 常见坑（务必避开）

1. 所有属性都必须走 `--prop`，不存在 `--name` 这类参数。
2. `[N]` 路径整体作为一个参数；不要尝试让它经过 shell。
3. PPT 的 `shape[1]` 通常是标题占位符，正文内容从 `shape[2]` 起。
4. 不支持 `/shape[名字]`：用数字索引或 `@name=`。
5. 不要猜属性名 —— 先 `help`。
6. 文件被 Word/WPS/Excel 打开时先关闭，否则写入失败（file_locked）。
7. 文本换行：`\n` 新段落、`\v` 段内换行；字面反斜杠写 `\\n`。
8. `set` 不带 `--find` 时作用于整个元素；带 `--find` 只改匹配文本（自动拆分 run）。
9. `--find` 无匹配是静默成功，用 `--json` 看 `matched` 数量。
10. `batch` 默认**原子回滚**（任一失败全回滚）；要保留成功项加 `--best-effort`。
11. 单位/颜色写错不会报错但会静默忽略，改完必须 `view issues` 检查。
12. 需要非 officecli 程序（python-docx、渲染器、交付/上传）读取前，先 `save`（或 `close`）；本环境已禁用 resident，通常直接落盘。

## 8. 标准工作流程

1. **探查**：已有文件先 `view <file> outline`（+ `stats`），`get`/`query` 定位目标节点。禁止盲改。
2. **建/改**：结构先行（页面、样式、标题），再填内容，最后做格式；每步检查退出码。
3. **批量**：同一文件的多处改动合并成一次 `batch`，减少 open/save 次数。
4. **校验**：`validate <file> --json`；`view <file> issues`；`view <file> stats`。
5. **目检**：`view <file> html`（返回 HTML 路径）或 `view <file> screenshot -o out.png`；PPT 可用 `view <file> svg` 逐页检查。
6. **交付**：说明生成/修改的文件路径、结构概览、校验结论；失败就如实报告错误码与原因，不要谎报成功。

常见错误码：`file_not_found`（路径错）、`file_locked`（被 Office 占用，先关闭）、`file_exists`（create 不覆盖已有文件，改路径或加 `--force`）、`io_error`（含 TEMP 权限问题，见第 0 节，说明未走包装脚本）、`invalid_prop`（属性名或值格式不对，去查 `help`）。

## 9. 目录结构

```text
.agents/skills/officecli/
├── SKILL.md                        # 本文件
├── scripts/officecli.cmd           # 必需的环境包装脚本（TEMP 重定向 + 关闭 resident）
└── references/
    ├── official-manual.md          # 官方操作说明全文（命令、元素、批量、raw XML）
    ├── cheatsheet.md               # 一页纸命令速查
    └── skill-word.md / skill-pptx.md / skill-excel.md   # 内置写作规范离线副本
```
