# `ui/markdown.py` 逐段讲解

> 本篇讲终端 Markdown 渲染。上游是 `ui/app/live.py`（唯一调用方），下游是 `ui/style.py`（ANSI 样式与宽度计算）。

把模型的 Markdown 回复转成带 ANSI 样式的终端文本：自己用正则做行级解析（不转 rich 的 Markdown），输出纯字符串交给上层画帧。

- 行数：138 行
- 依赖：[`ui/style.py`](style.md) —— `bold` / `dim` / `italic` / `cyan` / `yellow` 等着色函数和 `display_width()` 宽度计算
- 被谁用：[`ui/app/live.py`](app-live.md) —— 流式帧收口与历史重放时调 `render_markdown()`

## 目录

- [1. 模块 docstring 与依赖](#1-模块-docstring-与依赖1–10-行)（1–10 行）
- [2. 语法正则表](#2-语法正则表12–23-行)（12–23 行）
- [3. `_split_row` 与 `_is_table_row`](#3-_split_row-与-_is_table_row26–33-行)（26–33 行）
- [4. `render_markdown`：按围栏切段](#4-render_markdown按围栏切段36–45-行)（36–45 行）
- [5. `_render_fence`：代码块](#5-_render_fence代码块48–55-行)（48–55 行）
- [6. `_render_inline`：行内样式](#6-_render_inline行内样式58–64-行)（58–64 行）
- [7. `_render_blocks`：块级逐行分派](#7-_render_blocks块级逐行分派67–100-行)（67–100 行）
- [8. `_render_table`：GFM 表格](#8-_render_tablegfm-表格103–138-行)（103–138 行）

## 1. 模块 docstring 与依赖（1–10 行）

docstring 划出支持范围：**围栏代码块、标题、引用、列表、链接/加粗/斜体/行内代码、GFM 表格**。

只依赖标准库 `re` 和 `ui.style`——**不引 rich**。这是刻意的选择，理由在后面第 5 节（流式输出到一半的 markdown）和第 8 节（表格宽度）。

导出只有一个公共函数 `render_markdown()`，其余全是 `_` 前缀的私有函数：模块对外就是一个"文本进、带颜色的文本出"的纯函数，不持有状态。

## 2. 语法正则表（12–23 行）

七个模块级正则，编译一次复用：

| 正则 | 匹配 | 行号 |
|---|---|---|
| `_FENCE` | 围栏代码块 ```` ```lang\n body ``` ````，`re.S` 让 `.` 跨行 | 13 |
| `_BOLD` | `**text**` | 14 |
| `_CODE` | `` `text` ``（禁止内部反引号，避免吃掉相邻代码段） | 15 |
| `_ITALIC` | `*text*`，前后各两个负向断言排除 `**` | 16 |
| `_LINK` | `[text](url)` | 17 |
| `_OL` | 有序列表 `1. text` | 18 |
| `_TABLE_ROW` | 管道行 `\| a \| b \|` | 19 |
| `_TABLE_SEP` | GFM 分隔行 `\|---\|---:\|` | 23 |

两点值得注意：

- `_FENCE` 用 `(\w*)` 抓语言标签、`(.*?)` 非贪婪抓正文。非贪婪是为了正确处理**多个连续代码块**——贪婪匹配会把两个块之间的正文一起吞进第一个块。
- `_TABLE_SEP`（20–23 行）上方有一段注释解释了一个 GFM 歧义：`| - |` 这种行既是合法的分隔行，也可能是数据行。这里**当分隔行处理**，理由是"与参考 GFM 解析器一致"。正则写成 `\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?` 支持首尾竖线可选、对齐冒号可选。

## 3. `_split_row` 与 `_is_table_row`（26–33 行）

两个表格辅助函数：

- `_split_row`（26–28 行）：按 `|` 拆单元格并 `strip()`。注意 `.group(1)` 拿的是 `|` 之间的内容，首尾的空白管道不产生空单元格。
- `_is_table_row`（31–33 行）：**是管道行但不是分隔行**。注释点明原因——分隔行也匹配 `_TABLE_ROW`，必须显式排除，否则分隔行会被当成数据行渲染出来。

## 4. `render_markdown`：按围栏切段（36–45 行）

入口函数，思路是**先用围栏把文本切成"块外"和"块内"交替的段**，再分别渲染：

```python
for match in _FENCE.finditer(text):
    chunks.append(_render_blocks(text[cursor : match.start()]))   # 围栏之前的正文
    chunks.append(_render_fence(match.group(1), match.group(2)))  # 围栏内的代码
    cursor = match.end()
chunks.append(_render_blocks(text[cursor:]))                      # 最后一段正文
```

设计意图：**代码块的优先级高于一切**。代码块内部不做行内/块级渲染（`**`、`#`、`|` 在代码里都当字面量），这个切段顺序天然保证了隔离——先切走代码，剩下的才做块级解析。

收尾 `return "\n".join(chunk for chunk in chunks if chunk != "")`：过滤空段。围栏紧贴文本时会产生空字符串段，不筛掉会多出空行。

## 5. `_render_fence`：代码块（48–55 行）

渲染成带边框的暗色框，标签默认 `"code"`：

```
┌ python
│ def f():
│     return 1
└
```

四个细节：

- `label = lang.strip() or "code"`：模型经常写 ```` ``` ```` 不带语言，兜底标签保证边框始终有头。
- `body.rstrip("\n")` 再 `splitlines()`：去掉围栏闭合前的尾换行，但**保留内部空行**（`splitlines()` 对空行产出空串）。
- `or [""]`：空代码块（```` ``` ```` 紧贴 ```` ``` ````）也要画出 `│` 行，否则框是空的。
- 整框用 `style.dim`：代码块整体压暗，和正文的高亮（加粗/青色标题）形成层次。

**没有语法高亮。** docstring 里列的"代码块高亮"实际只做到"标签 + 暗色框"这一层——这是明确的取舍：语法高亮要引入 lexer（Pygments 之类），而项目运行时依赖只有 `openai` 和 `python-dotenv` 两个包。语言标签 + 视觉分区已经能让人一眼看出这是代码块。

## 6. `_render_inline`：行内样式（58–64 行）

四步串行替换，**顺序有讲究**：

```python
_LINK    → style.cyan(文字)     # 链接
_BOLD    → style.bold()         # 加粗
_CODE    → style.yellow()       # 行内代码
_ITALIC  → style.italic()       # 斜体
```

为什么这个顺序：

1. **链接最先**：`_LINK` 只保留 `m.group(1)`（显示文字），**丢弃 URL**。终端里 URL 不能点，打印出来是噪音。先丢掉 URL 后面的三个正则就不会误伤 URL 里的 `*` 和 `_`。
2. **加粗在斜体之前**：`**bold**` 若先过 `_ITALIC`，两个 `*` 之间会匹配出错。虽然 `_ITALIC` 有负向断言保护，但先处理更粗粒度的 `**` 更稳。
3. **行内代码最后之一**：`` `code` `` 里的内容不再被后面的斜体规则动。

`_ITALIC` 的正则本身值得单说：`(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)`——前后各四个断言，确保匹配的 `*` 单侧不是另一个 `*`。代价是**连续斜体段**（`*a* *b*`）能正常工作，但 `**a* b**` 这类嵌套写法不保证正确。

## 7. `_render_blocks`：块级逐行分派（67–100 行）

块级渲染的主循环，逐行 `startswith` 分派：

| 行首 | 渲染 | 行号 |
|---|---|---|
| 表格行 + 下一行是分隔行 | 调 `_render_table` | 79–86 |
| `### ` | `bold(行内渲染)` | 87–88 |
| `## ` / `# ` | `bold(cyan(行内渲染))` | 89–91 |
| `> ` | `dim("│ ") + italic(行内渲染)` | 92–93 |
| `- ` / `* ` | `dim("• ") + 行内渲染` | 94–95 |
| 数字 `. ` | `dim("1. ") + 行内渲染` | 97–99 |
| 其他 | 仅行内渲染 | 100–101 |

几个设计点：

- **标题分两级视觉**：一级/二级用 `bold(cyan)`（醒目），三级用 `bold`（弱一档）。终端没有字号，只能靠颜色 + 粗体拉开层次。
- `body = _render_inline(raw[raw.index(" ") + 1:])`（90 行）：用 `index(" ")` 定位第一个空格，一次兼容 `# ` 和 `## ` 两种前缀长度，省一个分支。
- **引用用 `│` 而不是 `>`**：`>` 在终端里容易和 shell 提示符、diff 输出混淆，`│` 是明确的竖条引用线。
- **列表符号替换成 `•`**：`*` 在 Markdown 里既是列表符又是强调符，换成 `•` 消除歧义。

**表格的多行前瞻**（79–86 行）是这里唯一跨行的逻辑：

```python
if _is_table_row(raw) and i + 1 < len(src) and _TABLE_SEP.match(src[i + 1]):
    j = i + 2
    while j < len(src) and _is_table_row(src[j]):
        j += 1
    lines.append(_render_table(src[i:j]))
    i = j
```

先看当前行是管道行、且**下一行是分隔行**，才认定这是表格；然后一直吃到第一个非管道行为止。整个表格（表头 + 数据行）一次性交给 `_render_table`，`i = j` 跳过已消费的行。

## 8. `_render_table`：GFM 表格（103–138 行）

最复杂的一段，docstring 解释了三个关键决策：

**决策一：列宽一律用原始文本算**（120–123 行）

```python
widths[c] = max(widths[c], style.display_width(cell))
```

注释写得很直白："从不用样式后的文本——ANSI 转义会干扰宽度计算"。`display_width()` 内部会先 `strip_ansi()` 再去算 CJK 宽字符，所以传原始文本是对的。

**决策二：填充在加样式之前算好**（127–132 行）

```python
rendered = style.bold(_render_inline(cell)) if ridx == 0 else _render_inline(cell)
pad = " " * max(0, widths[c] - style.display_width(rendered))
```

行内渲染会**缩短文本**（`[a](http://...)` 丢掉 URL、`` `a` `` 去掉反引号），所以 padding 必须按**渲染后**的宽度补到**原始**列宽上。两个宽度分开算、一次补齐，结果是"有颜色和没颜色对齐都不跑"。

**决策三：边框用 ASCII `+---+` 而不是 Unicode 框线**（133 行及上方注释）

这是全模块最有意思的一条注释：

> U+2500 框线字符在 CJK 终端按 2 宽、其他环境按 1 宽，一旦列宽计算遇上中文 locale，unicode 网格就错位。

`┌─┬─┐` 在中文终端里会被渲染成双宽字符，而 `display_width` 对 `U+2500` 的判定（见 `style.cell_width` 的 `A` 歧义宽处理）在不同 locale 下不一致——**宽度算错，表格就整个错位**。用 ASCII 的 `+` 和 `-` 是确定性选择。对照第 5 节代码块的 `┌ │ └`：那里是单列框，宽度不参与对齐，所以能用 Unicode。

**其他细节**：

- **分隔行在 `_render_table` 内部丢弃**（114–116 行，注释明说"而不是调用方"）：调用方用 `_is_table_row` 过滤时已经排除了分隔行，但这里再兜一层——表中间混进的分隔行能优雅降级，不会渲染成一行 `---`。
- **列数对齐**（117–119 行）：`ncols = max(...)` 取最宽的行，短行补空字符串。模型经常输出列数不齐的表格，不补会 `IndexError`。
- **表头加粗**（127 行）：`ridx == 0` 的行走 `style.bold`。
- **表头后多插一条横线**（134–135 行）：`hborder` 在首行后重复一次，形成 GFM 的标准双线表头。
- **空表格降级**（115–116 行）：`if not parsed` 就退化成逐行行内渲染，不当表格画。
