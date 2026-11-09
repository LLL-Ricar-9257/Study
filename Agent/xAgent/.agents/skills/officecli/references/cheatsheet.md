# officecli 一页纸速查

> 全部命令都通过包装脚本调用（自动修正 TEMP、关闭 resident）：
> `command: <工作区>\..\.agents\skills\officecli\scripts\officecli.cmd`，`args` 逐项传入。
> 本文示例为 `args` 数组形式（省略引号，因为 Runcil 不经过 shell）。

## 0. 先看帮助（不确定就查，别猜）

```json
["help"]
["help", "docx"]                          // 元素列表
["help", "xlsx", "cell"]                  // 元素 schema
["help", "pptx", "set", "shape"]          // 该元素可用于 set 的属性
["help", "docx", "paragraph", "--json"]
```

## 1. 读取与探查（L1）

```json
["create", "out/a.docx"]                                  // 新建空白（按扩展名判类型）
["view", "a.docx", "outline"]                             // 结构树
["view", "a.docx", "stats"]                               // 字数/页/形状统计
["view", "a.docx", "text", "--start", "1", "--end", "80"] // 纯文本
["view", "a.docx", "annotated"]                           // 带格式标注的文本
["view", "a.docx", "issues"]                              // 格式/内容/结构问题清单
["view", "a.docx", "html"]                                // 静态 HTML 快照（返回路径）
["view", "a.pptx", "screenshot", "-o", "out/s1.png"]
["view", "a.pptx", "pdf", "-o", "out/a.pdf"]
["get", "a.docx", "/body/p[1]", "--depth", "2", "--json"]
["query", "a.docx", "paragraph[style=Heading1]", "--json"]
["query", "a.xlsx", "Sheet1!row[Salary>5000]"]
["validate", "a.docx", "--json"]
["dump", "a.docx", "/body"]                               // 导出可回放 batch JSON
```

## 2. 编辑（L2）

```json
["set", "a.docx", "/body/p[1]", "--prop", "bold=true", "--prop", "color=red"]
["set", "a.docx", "/", "--find", "draft", "--replace", "final"]
["set", "a.docx", "/body/p[1]", "--find", "\\d+%", "--prop", "regex=true", "--prop", "color=red"]
["add", "a.docx", "/body", "--type", "paragraph", "--prop", "text=标题", "--prop", "style=Heading1"]
["add", "a.docx", "/body", "--type", "table", "--prop", "rows=3", "--prop", "cols=3"]
["add", "a.docx", "/body/p[1]", "--type", "run", "--after", "find:天气", "--prop", "text=（晴）"]
["add", "a.pptx", "/slide[1]", "--type", "shape", "--prop", "text=要点", "--prop", "x=2cm", "--prop", "y=5cm"]
["add", "a.xlsx", "/Sheet1", "--type", "chart", "--prop", "type=bar", "--prop", "source=Sheet1!A1:B10"]
["remove", "a.docx", "/body/p[4]"]
["move", "a.docx", "/body/p[4]", "--after", "/body/p[1]"]
["swap", "a.pptx", "/slide[1]/shape[2]", "/slide[1]/shape[3]"]
```

## 3. 批量（一次 open/save，推荐）

```json
["batch", "a.xlsx", "--json", "--commands", "[\n {\"command\":\"set\",\"path\":\"/Sheet1/A1\",\"props\":{\"value\":\"姓名\",\"bold\":\"true\"}},\n {\"command\":\"set\",\"path\":\"/Sheet1/A2\",\"props\":{\"value\":\"Alice\"}}\n]"]
```

- 默认原子：任一项失败整批回滚（`atomicRolledBack: true`）。
- `--best-effort`：保留成功项；`--stop-on-error`：遇错即停。
- 也可把数组写入 JSON 文件，用 `--input out/ops.json`。

## 4. 底层 XML（L3，L2 表达不了时）

```json
["raw", "a.docx", "/document"]
["raw-set", "a.docx", "/document", "--xpath", "//w:body/w:p[1]", "--action", "replace", "--xml", "<w:p>...</w:p>"]
["add-part", "a.docx", "/"]
```

`--action`：`append|prepend|insertbefore|insertafter|replace|remove|setattr`。

## 5. 值格式

| 类别 | 支持写法 |
|---|---|
| 颜色 | `FF0000` `#FF0000` `red` `rgb(255,0,0)` `accent1`…`accent6` |
| 间距 | `12pt` `0.5cm` `1.5x` `150%` |
| 尺寸 | `914400`（EMU）`2.54cm` `1in` `72pt` `96px` |
| 文本 | `\n` 新段落、`\v` 软换行、`\t` 制表符 |

## 6. 路径速记

| 格式 | 常用路径 |
|---|---|
| docx | `/`、`/body`、`/body/p[N]`、`/body/tbl[N]/tr[R]/tc[C]`、`/header[1]`、`/footer[1]`、`/styles` |
| xlsx | `/Sheet1`、`/Sheet1/A1`、`/Sheet1/row[3]`、`/Sheet1/col[C]`、`/sheet[N]` |
| pptx | `/`、`/slide[N]`、`/slide[N]/shape[M]`、`/slide[N]/shape[@name=Title 1]` |
| 稳定 ID | `shape[@id=550950021]`、`p[@paraId=1A2B3C4D]`、`comment[@commentId=1]` |

索引规则：路径 `[N]` 1-based；`--index` 0-based；**xlsx `add --type row|col` 的 `--index` 为 1-based**。

## 7. 交付前检查清单

1. `validate <file> --json` → `success: true`
2. `view <file> issues` → 无残留问题
3. `view <file> stats` / `outline` → 结构、字数符合预期
4. `view <file> html` 或 `screenshot` → 无占位符、无溢出、无 `\$` `\n` 字面量
5. 报告中给出文件路径 + 校验结论
