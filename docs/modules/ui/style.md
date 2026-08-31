# `ui/style.py` 逐段讲解

> 本篇讲终端样式与页脚。上游是 ui 各模块（渲染、命令、REPL 都 import 它），下游是终端本身（ANSI 转义序列）。

一句话职责：ANSI 颜色、显示宽度计算（CJK 感知）、滚动区内写入、固定在底部的页脚（计划/分隔线/目录/计量）。

- 行数：551 行
- 依赖：标准库（`atexit` / `os` / `re` / `shutil` / `sys` / `threading` / `unicodedata`），无项目内 import
- 被谁用：[ui/app/__init__.py](app.md)、[ui/app/live.py](app-live.md)、[ui/app/commands.py](app-commands.md)、[ui/app/refine.py](app-refine.md)、[ui/repl.py](repl.md)——**所有要往终端写东西的地方**

这是一篇**参考型**模块：大部分内容是「函数/常量清单 + 行为约定」，不是叙事型的逐段流水账。按功能分组讲，每组带行号。

## 目录

- [1. 颜色开关与终端尺寸](#1-颜色开关与终端尺寸14-55-行)
- [2. 滚动区写入：`stream_write` 与 CRLF](#2-滚动区写入stream_write-与-crlf57-84-行)
- [3. `_wrap` 嵌套样式修复与 `strip_ansi`](#3-_wrap-嵌套样式修复与-strip_ansi87-100-行)
- [4. CJK 感知的宽度计算](#4-cjk-感知的宽度计算102-196-行)
- [5. `replace_last_rows`：原位重写](#5-replace_last_rows原位重写199-222-行)
- [6. 颜色函数、banner 与 frame](#6-颜色函数banner-与-frame225-278-行)
- [7. `Footer` 类：固定页脚](#7-footer-类固定页脚285-551-行)

---

## 1. 颜色开关与终端尺寸（14–55 行）

`_ANSI_RE`（14 行）：匹配 ANSI CSI 序列 `\033[...X`，后面所有宽度计算和截断都靠它识别转义。

```python
def enabled() -> bool:
    if os.getenv("NO_COLOR"): return False
    if os.getenv("WHEEL_COLOR", "").lower() in {"0", "false", "no"}: return False
    return sys.stdout.isatty()
```

三个关闭条件：**`NO_COLOR` 环境变量**（[no-color.org](https://no-color.org) 约定，CI/管道里设了就不出颜色）、**`WHEEL_COLOR=0|false|no`**（项目专用开关）、**stdout 不是 TTY**（重定向到文件时不出颜色，防 ANSI 码污染日志）。

```python
def term_size() -> tuple[int, int]:
    # 试多个 fd：pty/harness 环境下 stdin 和 stdout 可能在不同设备上
    for stream in (sys.stdout, sys.stdin, sys.__stdout__, sys.__stdin__):
        ...
        size = os.get_terminal_size(fd)
        if size.lines > 0 and size.columns > 0: return ...
    return shutil.get_terminal_size(fallback=(80, 24))
```

**不信 `COLUMNS` 环境变量**（可能过期），从 tty ioctl 直接读。试 4 个 fd（`sys.stdout`/`sys.stdin` 和 `sys.__stdout__`/`sys.__stdin__` 原始引用）——pty/harness 环境下 stdin 和 stdout 可能在不同设备上、或其中一个被捕获，第一个报出合理尺寸的就用。全失败兜底 80×24。

## 2. 滚动区写入：`stream_write` 与 CRLF（57–84 行）

```python
_ACTIVE_FOOTER: Footer | None = None   # 当前激活的页脚
OUTPUT_LOCK = threading.RLock()        # 保护并发写 stdout
```

两个模块级全局：`_ACTIVE_FOOTER` 让 `stream_write` 知道「有没有固定输入行要保护」；`OUTPUT_LOCK` 是**写 stdout 的互斥锁**——页脚绘制（`Footer.paint`）和流式输出（`on_delta`）在不同线程跑，不加锁会交错出花屏。`RLock` 因为 `paint` 内部会调 `stream_write`（重入）。

```python
def stream_write(text: str) -> None:
    with OUTPUT_LOCK:
        footer = _ACTIVE_FOOTER
        pinned = footer is not None and footer.input_text is not None and is_tty()
        if pinned:
            sys.stdout.write("\0338")   # DECRC：恢复「流光标」
        sys.stdout.write(text)
        sys.stdout.flush()
        if pinned:
            sys.stdout.write("\0337")   # DECSC：把流光标存回去
            footer._focus_input()
```

**固定输入行 `>` 的写入协议**：busy 时页脚多一行显示已敲的文本（`input_text`）。流式输出写内容会移动光标，写完必须把光标放回 `>` 行末尾（`_focus_input`），否则输入回显会跑到内容上。`\0337`/`\0338`（DECSC/DECRC，保存/恢复光标）是实现：写之前恢复流光标位置、写之后存回去。

`crlf`（82–84 行）：统一换行为 CRLF。注释点破：**raw 模式（行编辑器）下 `\n` 不会自动变成 `\r\n`**，终端会把 `\n` 当纯换行（不回车），导致文字从行首下一行错位。所有写入必须走 CRLF。

## 3. `_wrap` 嵌套样式修复与 `strip_ansi`（87–100 行）

`_wrap`（87–95 行）是所有颜色函数的核心：

```python
def _wrap(code: str, text: str) -> str:
    if not enabled() or not text:
        return text
    inner = text.replace("\033[0m", f"\033[0m\033[{code}m")
    return f"\033[{code}m{inner}\033[0m"
```

**嵌套样式的修复**：`bold(cyan("x"))` 展开是 `\033[1m\033[36mx\033[0m\033[0m`——内层 `cyan` 的 `\033[0m` 会把外层 `bold` 一起重置掉。`_wrap` 把内层的 `0m` 替换成 `0m` + 重设本码，保住嵌套。这是 ANSI 颜色最常见的坑。

`strip_ansi`（97–100 行）：用 `_ANSI_RE` 去掉所有转义得到纯文本——后面所有宽度计算都先过它。

## 4. CJK 感知的宽度计算（102–196 行）

**这是整个模块最关键的逻辑**——终端按「格」排布，CJK 字符占 2 格，算错宽度就错位。

```python
def cell_width(ch: str) -> int:
    if not ch or ch in "\n\r": return 0
    if unicodedata.combining(ch): return 0
    return 2 if unicodedata.east_asian_width(ch) in {"W", "F", "A"} else 1
```

- 组合字符（combining，如重音符号）算 0（附着在前一字符上，不占格）。
- `east_asian_width` 是 `W`（宽）/ `F`（全角）/ `A`（**歧义**）算 2。注释点破 `A` 的处理：**歧义宽（制表符、`─` 等）保守算 2**——CJK 终端把 U+2500 渲染成宽字符，一条按 1 格算的 `cols` 长 `─` 会在 CJK locale 下换行、把后面的行挤到右边。宁可少填不可溢出。

`display_width`（113 行）：先去 ANSI 再逐码点累加。

`rule_line`（118 行）：用 **ASCII 短横线** `-` 而不是 box-drawing `─`——注释点破：`U+2500` 是歧义宽，中文 locale 下会换行。分隔线用 ASCII 最稳。

`display_rows`（123 行）：文本在 `cols` 宽度下占多少终端行（含自动换行）。空行算 1 行，非空行 `(width-1)//cols + 1`。

`fit_display`（142–165 行）：把文本截到 `cols` 宽度内，**保留 ANSI、不截半截转义序列**——遇到 `\033` 用 `_ANSI_RE.match` 匹配整段转义原样保留，遇到普通字符按 `cell_width` 累加，超了停。

`wrap_display`（167–184 行）：按显示宽度折行（CJK 安全，不截半截码点）。循环 `fit_display` 取一段、剥掉、续，直到空。

`writeln_wrapped`（186–197 行）：按 `cols-1` 折行后写入。注释点破：**让终端自己永不自动换行**——自己折到 `cols-1` 格，终端就不会在 `cols` 格处硬换，避免「自己折的行」和「终端折的行」叠在一起错位。

## 5. `replace_last_rows`：原位重写（199–222 行）

```python
def replace_last_rows(row_count, new_text, *, reserved_bottom=None):
    # 擦掉最后 row_count 行，在原位写入 new_text
```

非 TTY：直接写新文本（无滚动区概念）。TTY：算可用行 `limit = rows - reserved_bottom - 1`，**要擦的行比可用行还多时不逐行上移、直接换行重写**（防擦到页脚上）。否则用 `\r\033[2K`（回车清当前行）+ `\033[1A\033[2K` × (n-1)（逐行上移清行）擦掉，再写新文本。

`reserved_bottom` 缺省 `Footer.HEIGHT`——页脚占 3 行，擦行不能擦进页脚区。

## 6. 颜色函数、banner 与 frame（225–278 行）

八个颜色函数（225–255 行）：`bold`(1) / `dim`(2) / `italic`(3) / `cyan`(36) / `green`(32) / `yellow`(33) / `red`(31) / `magenta`(35)，都只调 `_wrap`。语义约定：

| 颜色 | 用途 |
|---|---|
| `dim` | 次要信息（路径、提示、时间线） |
| `cyan` | 用户操作回显（steer/follow/plan 步骤） |
| `green` | 成功（切换成功、undo 完成、resume） |
| `yellow` | 询问（y/N 提示） |
| `red` | 错误/失败 |
| `bold` + `cyan` | 强调（banner、进行中的 plan 步骤） |

`banner`（257–268 行）：启动横幅的 ASCII 框图，上下框线 `bold(cyan)`、中间 `dim`。

`prefix_block` / `frame`（270–278 行）：把「标签 + 已渲染的正文」包成带框的块（`┌ label` / 正文 / `└`）。`frame` 的注释点破：「wrap **already-rendered** text without recoloring each line」——正文可能已经是 markdown 渲染过的带色文本，`frame` 只包框不重新着色。

`_clear_rows`（280–282 行）：逐行定位清行（`\033[r;1H\033[2K`），Footer 变矮/disarm 时用。

## 7. `Footer` 类：固定页脚（285–551 行）

页脚固定在终端底部，`HEIGHT = 3`（分隔线 + 目录 + 计量 3 行），busy 时多 1 行（固定输入 `>`），有 plan 时再多 N 行（受终端高度截断）。

### 7.1 字段与高度计算（285–316 行）

```python
HEIGHT = 3
def __init__(self):
    self.text = ""               # 计量行主文本
    self.cwd = ""                # 工作目录
    self.plan_lines: list[str] = []   # plan 步骤（进行中的高亮）
    self._armed = False          # 是否已预留滚动区
    self._pinned = 0             # 当前预留高度
    self._size_armed: tuple[int, int] | None = None   # arm 时的尺寸
    self._resized = threading.Event()   # SIGWINCH 标记
    self.input_text: str | None = None  # 固定输入行 `>` 的内容（None = 不显示）
```

`_height_for(rows)`：给定终端行数算页脚能多高——plan 行在剩余空间里截断（`max_extra = rows - HEIGHT - input_h - 2`，留 2 行缓冲）。`height()` 对外暴露当前高度（行编辑器的 `reserved_bottom` 用它）。

### 7.2 固定输入行：`set_input` / `_focus_input`（318–358 行）

`set_input`：`None` 隐藏固定输入行 `>`，字符串（哪怕空）显示。显示时的关键操作（注释逐条点破）：

1. **种 DECSC 流光标**（`\033[row;1H\0337`）：在 `stream_row`（用户任务行下一行）存一个光标位置，让**回合输出从那里继续**，而不是跳到最后一个滚动行。
2. **行号随滚动上移**：显示输入行会让页脚变高、滚动区上移，`stream_row` 也要减掉变高量（`row = stream_row - grew`）。
3. 行号越界时兜底到 `bottom`（页脚顶行）。

`_focus_input`：把光标放到 `>` 行末尾（`> ` + 已输入文本之后），供 `stream_write` 写完内容后恢复。

### 7.3 `arm` / `_arm_locked`：预留滚动区（360–422 行）

`arm(reset=False)`：预留最后几行给页脚。`reset=True` 清屏从顶部开始。

`_arm_locked` 的核心是 **DECSTBM**（`\033[top;botr`，设置滚动区上下边界）——把滚动区上边界设到 `1`、下边界设到 `rows - h`（页脚顶），这样内容滚到页脚顶就停，页脚区不被覆盖。

几个细节：

- **终端太矮（`rows < 5`）不预留**：`HEIGHT=3` + 输入行 1 + 缓冲 2 = 最少 6 行才放得下，矮终端直接放弃页脚。
- **页脚变高**：内容上滚 n 行（`\033[nS`），**DECSC 存的流光标也要随滚动上移**（`\0338\033[nA\0337`），否则下一次流写入落在新页脚上。
- **页脚变矮**：清出多出来的行（`_clear_rows`）。
- **DECSTBM 会把光标送到滚动区底部**：所以先 DECSC 后 DECRC（存/恢复），让光标留在内容区。busy `>` 显示时**跳过 DECSC**——那个槽位存的是流光标，不能覆盖。
- **`reset=True` 先丢 DECSTBM 再清屏**（`\033[r\033[2J\033[H`）：注释点破——带过期滚动区的 `CSI 2 J`（清屏）会漏掉行，宽度变化后留下斜杠菜单残影。
- `arm` 末尾 `_ACTIVE_FOOTER = self`（`stream_write` 据此保护固定输入行）、`atexit.register(self.disarm)`（进程退出自动解除）。

### 7.4 `disarm`：解除预留（424–440 行）

清固定行（`_clear_rows(rows-h+1, rows)`）、恢复默认滚动区（`\033[r`）、恢复自动换行（`\033[?7h`）、恢复光标（`\0338`）、`_ACTIVE_FOOTER = None`。`shutdown_ui`（[ui/app/__init__.py](app.md) 6.4 节）退出前调它。

### 7.5 `set` / `notify_resize` / `consume_resize` / `relayout`（442–476 行）

`set(text, cwd=...)`：更新计量行主文本和目录，重绘。

`notify_resize`：SIGWINCH 处理设标记（[ui/app/__init__.py](app.md) 6.3 节注册了信号 handler）。

`consume_resize(reset=False)`：消费标记，**尺寸确实变了才重排**（`_size_armed != (rows, cols)`），返回是否重排。空闲时行编辑器轮询它（`on_prompt_idle`）。

`relayout`：尺寸变了重新 `_arm_locked` + 重绘。注释点破：**空闲时的 SIGWINCH 只挪 DECSTBM 和页脚，不清空对话**——让终端能重新折已打印的回合，而不是 `reset=True` 把屏清空。

### 7.6 `paint` / `_paint_locked`：重绘整个页脚（478–551 行）

`paint`：加 `OUTPUT_LOCK` + `self._lock`，尺寸变了先重新 `arm` 再 `_paint_locked`。

`_paint_locked` 是重绘核心，拼一组 ANSI 序列一次写出：

```python
usable = max(1, cols - 1)
h = self._height_for(rows)
# plan 行放不下时截断加省略号：shown = shown[:extra-1] + ["…"]
# 进行中的 plan 步骤（含 "[>]"）bold(cyan) 高亮，其余 dim
parts = ["\033[?7l"]                    # 关自动换行（DECAWM）
if self.input_text is None: parts.append("\0337")   # 存流光光标（busy 时跳过——槽位存的是流光标）
start = rows - h + 1
for i, ln in enumerate(plan):
    parts.append(f"\033[{start+i};1H\033[2K{ln}")   # 逐行定位清行写 plan
# 固定输入行 `>`：bold(cyan("> ")) + 已输入文本
parts.append(f"\033[{rows-3};1H\033[2K{mark}{typed}")
parts.append(f"\033[{rows-2};1H\033[2K{rule}")   # 分隔线
parts.append(f"\033[{rows-1};1H\033[2K{cwd}")    # 目录
parts.append(f"\033[{rows};1H\033[2K{body}")     # 计量行
if self.input_text is None: parts.append("\0338")   # 恢复流光标
else: parts.append(f"\033[{rows-3};{col}H")         # 光标回 `>` 末尾
parts.append("\033[?7h")                    # 恢复自动换行
```

两个关键决策（注释点破）：

- **关 DECAWM（`\033[?7l`）**：数错的字符不能把滚动区换行——重绘期间临时关自动换行，定位写完后恢复。
- **busy 时 DECSC 槽位存的是流光标**：`paint` 里 `if self.input_text is None: parts.append("\0337")` 的条件就是这个——busy 时不存流光标（已经有了），结束时也不恢复（`else` 分支把光标定位到 `>` 末尾）。

`prompt_rule`（536–540 行）：输入提示前的分隔线，`cols-1` 宽、`dim`。
