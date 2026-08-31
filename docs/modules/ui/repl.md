# `ui/repl.py` 逐段讲解

> 本篇讲终端输入层 `ui/repl.py`。上游是 [ui/app.md](app.md)（进程主循环把它接进事件流和页脚），下游是 [ui/style.md](style.md)（终端尺寸、ANSI、页脚）和 [tools/atfiles.md](../tools/atfiles.md)（@ 补全）。

把"怎么从终端读进一行"这件事从业务里切出来：斜杠命令目录、自绘行编辑器、busy 期间的固定输入行、方向键选择器，四组原语。

- 行数：897 行
- 依赖：
  - [ui/style.md](style.md) —— 终端尺寸、显示宽度计算、折行、ANSI 上色、页脚 `Footer`
  - [tools/atfiles.md](../tools/atfiles.md) —— `@` token 定位/替换与候选路径列表
  - [tools/rgfiles.md](../tools/rgfiles.md) —— 经 atfiles 间接用到的 glob（有 ripgrep 就用）
- 被谁用：
  - [ui/app.md](app.md) —— `session()` 里构造 `LineEditor` / `BusyPrompt`，主循环调 `editor.read()` 和 `busy_wait()`
  - [ui/app-commands.md](app-commands.md) —— `/resume` `/tree` `/graph` 的列表选择用 `pick_list`
  - `ui/app/__init__.py:152` 的 `ask_yes_no` 也走同一套 y/N 输入（但实现在 app 里，不在本文件）

## 目录

- [1. 斜杠命令目录与前缀匹配](#1-斜杠命令目录与前缀匹配2056-行)
- [2. 三列菜单排版](#2-三列菜单排版73--113-行)
- [3. 输入暂存与字节读取](#3-输入暂存与字节读取115--165-行)
- [4. CSI 解码与 Enter 语义](#4-csi-解码与-enter-语义166--203-行)
- [5. 多行缓冲的视觉折行与光标定位](#5-多行缓冲的视觉折行与光标定位204--252-行)
- [6. DSR 光标行查询](#6-dsr-光标行查询253--300-行)
- [7. 按键解码](#7-按键解码301--358-行)
- [8. busy 模式的终端设置与 `BusyPrompt`](#8-busy-模式的终端设置与-busyprompt354--417-行)
- [9. `LineEditor`：构造与补全](#9-lineeditor构造与补全419--483-行)
- [10. 调色板与 readline 补全器](#10-调色板与-readline-补全器484--510-行)
- [11. 入口分发与页脚高度协商](#11-入口分发与页脚高度协商511--556-行)
- [12. 自绘编辑主循环](#12-自绘编辑主循环557--658-行)
- [13. 菜单重绘与历史导航](#13-菜单重绘与历史导航659--723-行)
- [14. 提交与绘制](#14-提交与绘制724--825-行)
- [15. `pick_list` 方向键选择器](#15-pick_list-方向键选择器827--881-行)
- [16. `completion_words` 词表拼装](#16-completion_words-词表拼装882--897-行)

## 函数/类速查表

| 名字 | 行号 | 职责 |
|---|---|---|
| `SLASH_CATALOG` | 21–52 | 斜杠命令目录：(命令, 说明, 用法)，`/help` 和 Tab 菜单的唯一数据源 |
| `COMMANDS` | 54 | 从目录抽出的命令名元组 |
| `slash_matches` | 57–71 | 前缀匹配斜杠命令，默认上限 12 条 |
| `_pad` | 73–79 | 按显示宽度右填充/截断 |
| `format_slash_menu` | 81–113 | 候选命令排成三列菜单，窄屏自动缩列 |
| `_fd_pending` | 115–122 | `select` 判断 fd 是否有数据 |
| `_INPUT_STASH` / `_stash_input` / `_pop_stashed` | 124–145 | 查询光标时吃掉的用户输入暂存区 |
| `_read_byte` | 147–155 | 读一字节（优先取暂存，超时返回 None） |
| `_utf8_len` | 157–164 | UTF-8 引导字节还要几个续字节 |
| `decode_csi` | 166–197 | CSI 序列 → 按键名（方向键/home/end/粘贴标记/Kitty 协议） |
| `enter_submits` | 199–202 | Enter 是否提交（粘贴中或后面还有字节就不提交） |
| `editor_visual` | 204–227 | 缓冲区折成视觉行 + 算出光标所在行/列 |
| `_cursor_pos` / `cursor_vert` | 229–251 | 偏移 → (行, 列)；上下键跨行移动 |
| `query_cursor_row` | 253–299 | DSR `\033[6n` 查询光标所在行 |
| `_read_key` | 301–352 | 读一个逻辑按键（UTF-8 / ESC / CSI / Kitty） |
| `is_busy_abort_key` | 354–357 | busy 时算中止键的（Ctrl+C / Esc） |
| `enter_busy_tty` | 359–371 | busy 模式的终端设置（cbreak + 清 ISIG，保留 ONLCR） |
| `BusyPrompt` | 373–417 | 固定在页脚上方的 `>`，按键不回显进 say 块 |
| `LineEditor` | 419–825 | 行编辑器：历史、Tab 补全、@ 补全、多行、菜单 |
| `pick_list` | 827–880 | 方向键选择器（`/provider` `/resume` 等） |
| `completion_words` | 882–897 | Tab 补全词表 = 命令 + provider/effort 变体 + skill 名 |

---

## 1. 斜杠命令目录与前缀匹配（20–71 行）

`SLASH_CATALOG`（21–52 行）是**三元组表**：`(命令, 一句话说明, 用法)`。它是 `/help` 文本、Tab 菜单、`format_slash_menu` 三处的唯一数据源——加命令只改这张表。注意表里存的是**完整命令串**，包含带空格的二级命令（`/replay session`、`/refine auto`、`/jobs kill`、`/graph html`），这样前缀匹配能直接命中 `/repl<Tab>` → `/replay` 之外的 `/replay session`。

`COMMANDS`（54 行）只是从目录抽出第一列。上面那行注释点明了一个产品决定：**只有 `/` 开头才算命令，裸词一律是任务文本**。早期版本可能支持过 `help` 这种裸词，现在明确砍掉，因为"用户想让模型干一件事"远比"用户想敲命令"常见，裸词当命令会吞掉正常任务。

`slash_matches`（57–71 行）做前缀匹配，`limit=12` 是作者设定：菜单最多 12 行，再多用不上（真要找全表就按 `/help`）。`words` 参数允许传入外部词表——这让 [ui/app.md](app.md) 里动态生成的 `/provider <name>`、`/skill:<name>` 变体也能走同一套匹配逻辑。

非 `/` 开头的输入直接返回 `[]`，这是"命令"和"任务"的判定边界。

## 2. 三列菜单排版（73–113 行）

`_pad`（73–79 行）按**显示宽度**填充而不是字符数——CJK 一个字占两列，按 `len()` 填会歪。

`format_slash_menu`（81–113 行）把候选排成 命令/说明/用法 三列。列宽先取内容实际最宽，再各自钳到上限（命令列 8–22，说明列 4–18），然后两个 `while` 循环在总宽度超预算时**先缩说明列、再缩命令列**，用法列吃剩余空间。

这个顺序是有意的：命令列是用户唯一要精确读的（要按 Tab 选中它），说明列只是提示，砍它代价最小。选中行前缀是 `>`，其余是空格——高亮交给调用方按 `line.lstrip().startswith(">")` 上色（见 817 行）。

`style.fit_display(line, cols)` 是最后一道保险：无论如何不超出终端宽度。**多一列就会让终端自动折行，而 DECSTBM 底行折行会滚出第二个菜单**（后面 765 行的注释说的就是这事）。

## 3. 输入暂存与字节读取（115–165 行）

这一段是全文件最微妙的地方。

`_fd_pending`（115–122 行）用 `select` 探测 fd 是否可读，返回 bool 而不读走数据。

`_INPUT_STASH`（124–145 行）是一个 `deque[int]` 加一把锁。存在的理由写在注释里：**查询光标位置（DSR）和用户键入共用同一个 fd**。终端回的 DSR 报告是 `\033[<row>;<col>R`，要把它从输入流里认出来，就只能逐字节读——而读的过程中如果用户正好在打字，就把用户的输入也吃掉了。

所以：

- `_stash_input` 把"吃掉但不是报告"的字节存进 deque；
- `_read_byte`（147–155 行）**优先从 deque 取**，取不到才真去 `os.read(fd, 1)`。

存 `int` 而不是 `bytes` 是因为 `bytes` 迭代出来就是 int，`extend(data)` 直接展开，pop 时再 `bytes([x])` 包回一字节。

为什么这么麻烦而不直接发个查询？因为**真终端毫秒级就回，pty（测试 harness / 管道）永远不回**——不回的情况下整个等待窗口都在吃用户的输入，一整行就没了。暂存区把这个失败模式从"丢输入"降级成"查询返回 None"。

`_utf8_len`（157–164 行）只处理 3 字节（`0xE0`）和 4 字节（`0xF0`）引导，其余（含 2 字节 `0xC0`）落回 `return 1`。这是**有意的简化**：CJK 在 3 字节区间、emoji 在 4 字节区间，这两个是输入法会真实产生的；2 字节区间（拉丁扩展、希腊、西里尔）在本项目里基本不出现，但真出现时会少读一个续字节而解出乱码——这是已知缺口。

## 4. CSI 解码与 Enter 语义（166–203 行）

`decode_csi`（166–197 行）把 CSI 序列翻成按键名：

- `A/B/C/D/H/F` → 方向键 + home/end；
- `~` 结尾的：`200`/`201` 是**括号粘贴的开始/结束标记**（终端用 `\033[200~` 包住粘贴内容，这样程序能区分"粘贴的换行"和"用户敲的 Enter"），`3` 是 delete，`1/7` 是 home，`4/8` 是 end（两种终端各发一种）；
- `u` 结尾的是 **Kitty 键盘协议**：`\033[<code>;<mods>u`，13 = Enter、27 = Esc、其余是可打印 ASCII 码。注释特意点明"带修饰的 Enter 必须提交而不是中止"——否则在开了 Kitty 协议的终端（Kitty、WezTerm、Ghostty）里按 Enter 会被当成 Esc 处理。

`enter_submits`（199–202 行）把"Enter 该不该提交"抽成一个纯函数，两个条件：**粘贴中不提交**（粘贴的多行内容要整体进缓冲区），**后面还有字节时不提交**（说明这是 Shift+Enter 的 `\n`，不是单独 Enter）。

## 5. 多行缓冲的视觉折行与光标定位（204–252 行）

`editor_visual`（204–227 行）是整个多行编辑的显示基础：把带 `\n` 的缓冲区按 `wrap_display` 折成**视觉行**列表，同时算出光标在第几个视觉行、第几列。

它先用 `_cursor_pos`（229–238 行）把字节偏移 `cur` 翻成 (逻辑行号, 列)，再对每个逻辑行折行、累加，定位光标落在哪条折行片段上。返回 `(rows, cur_row, cur_col)` 给 `_draw_line` 用于光标定位（CUP 序列）。

`cursor_vert`（240–251 行）做上下键跨行：目标行是 `line_i + delta`，列取 `min(col, len(目标行))`（贴边而不是报错）。**单行缓冲区返回 `None`**——这是给上层的一个信号：单行时上下键不该走"跨行移动"，而该走"历史导航"。`_read_tty` 靠这个返回值三选一（菜单移动 / 跨行移动 / 翻历史）。

## 6. DSR 光标行查询（253–300 行）

`query_cursor_row`（253–299 行）向终端发 `\033[6n` 问"光标在第几行"。为什么需要它：页脚用 DECSTBM 固定在屏幕底部，输入行必须画在页脚**上方**，而"上方"是屏幕的哪一行取决于当前输出到哪了——只能问终端。

四道守卫：

1. 非 TTY 直接 `None`（管道/pty 永不回答，还会把转义序列漏进 harness 的输入里）；
2. `select` 已有数据待读时返回 `None`——输入在途就不问，避免吃输入；
3. `_INPUT_STASH` 非空时返回 `None`——上次查询已经吃掉过输入，先让读取者消化完；
4. 写查询要持 `style.OUTPUT_LOCK`，不能和工作线程的流式输出交错。

读循环用 `select(..., 0.05)` 逐字节等，见到 `\033[` 开头 + `;` + `R` 结尾就认作报告。解析失败（`ValueError`）时把整个 buf 都当输入暂存回去——说明那根本不是报告，是用户在打字。

## 7. 按键解码（301–358 行）

`_read_key`（301–352 行）从字节流里还原一个**逻辑按键**：

- 空读（`b""`）= EOF → 返回 `\x04`（Ctrl+D），统一成"文件结束"这一个信号；
- 高位字节 = UTF-8 引导，按 `_utf8_len` 收齐续字节后 decode；
- 普通字节直接 latin1 解；
- `ESC` 进入**消歧**：单独按 Esc 是一个字节单独到达，方向键的 `[` 几毫秒内就来，所以用 **30ms 窗口**区分。

消歧里有两处反直觉的处理，都是踩出来的：

```python
if nxt_byte != 0x5B:  # "["
    # ESC + 一个普通字节是带修饰的键，不是单独 ESC：很多终端把
    # Shift+Enter 发成 \x1b\r。这里返回 "esc" 会让每次 Shift+Enter
    # 都中止运行中的任务并让 UI 失同步。
    return nxt.decode("latin1")
```

很多终端把 Shift+Enter 发成 `\x1b\r`。若按"不是 CSI 就是 Esc"处理，用户每次换行都会中止任务——而且 UI 状态（页脚固定行、`_open` 块）还留在原地，这就是注释说的"UI 失同步"。所以 `ESC + 普通字节` 一律当"带修饰的键"，把那个字节原样返回（`\r` 就会被上层当 Enter 处理）。

`ESC + ESC` 返回 `"esc"`（真按了 Esc）；`ESC + UTF-8 引导字节` 是 Alt+非 ASCII，收完整字符返回。

CSI 部分循环收参数字节，见到 `0x40–0x7E` 区间的终止符就交给 `decode_csi`。参数超过 24 字节放弃返回 `esc`——防止畸形序列把读取卡死。

`is_busy_abort_key`（354–357 行）：busy 时只有 Ctrl+C 和 Esc 算中止键。Esc 也算是因为它在很多终端里是"取消"的肌肉记忆，而这里没有别的用途。

## 8. busy 模式的终端设置与 `BusyPrompt`（354–417 行）

`enter_busy_tty`（359–371 行）是这段最关键的一处：

```python
tty.setcbreak(fd)
attrs = termios.tcgetattr(fd)
attrs[3] &= ~termios.ISIG
termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
```

注释解释了为什么不直接用 `tty.setraw`：**setraw 会清掉 OPOST，而 OPOST 负责把 `\n` 翻译成 `\r\n`；清掉后 `print()` 的换行不回行首，say/think 框就成了楼梯式缩进**。所以这里用 `setcbreak`（只关回显和行缓冲）而不是 `setraw`，保留 ONLCR。

再清 `ISIG`：让 Ctrl+C 作为**字节 `\x03` 到达**而不是触发 SIGINT。这样 `busy_wait` 能在自己的循环里决定怎么处理它（中止任务），而不是被信号处理器打断在任意位置——工作线程还在跑，主线程被信号打断会留下不一致的终端状态。

`BusyPrompt`（373–417 行）是任务运行时固定在页脚上方的 `>`：

- 它**不是**一个完整的行编辑器：只支持退格、Ctrl+U 清空、可打印字符、Enter 提交。没有光标移动、没有历史、没有补全——因为 busy 时的正确心智是"敲一句话打断了它"，不是"编辑一段文本"。
- 每次敲键都调 `footer.set_input(self.buf)`，由页脚负责画（见 [ui/style.md](style.md) 的 `Footer.set_input` / `_focus_input`）。**按键永远不回显进 say 块**——流式输出和用户输入共用屏幕，回显会把两者搅在一起。
- `feed()` 返回非 None 表示提交了一整行。粘贴期间的 Enter 拼进 `buf` 当换行，不提交（和 `enter_submits` 同一套语义）。

## 9. `LineEditor`：构造与补全（419–483 行）

构造参数里四个注入点是它和 app 的全部耦合面：

| 参数 | 谁传 | 用途 |
|---|---|---|
| `words` | `_completion_words()`（[ui/app.md](app.md)） | Tab 补全词表，含 `/provider x`、`/skill:name` 变体 |
| `on_idle` | `on_prompt_idle` | 空闲回调，返回 True 表示"有事发生，重画一下" |
| `on_paint` | `STATE.footer.paint` | 每次重画后重绘页脚 |
| `at_files` | `list_at_files(workspace, tok)`（[tools/atfiles.md](../tools/atfiles.md)） | `@` 补全候选 |
| `reserved_bottom` | `STATE.footer.height` | 页脚占几行（可调用，因为页脚高度随计划行变化） |

**readline 依然是历史和补全的存储器**：构造时读 `~/.wheel_history`，`set_history_length(500)`，`atexit` 注册 `_save` 回写。自绘编辑器只是**绕过 readline 的输入循环**，历史数据仍从 `readline.get_history_item` 取（701–711 行），这样两套路径共享同一份历史。

兼容处理两处：`parse_and_bind("tab: complete")` 失败时退到 `"bind ^I rl_complete"`（老版 libedit 的语法）；`set enable-bracketed-paste on` 包在 try 里——不是所有 readline 都支持，失败就算了，自绘路径自己会发 `\033[?2004h`。

`prompt()`（491–496 行）用 `\001...\002` 包住 ANSI，这是 readline 的 `RL_PROMPT_START_IGNORE/END_IGNORE`——不包的话 readline 按字符数算光标位置，带颜色的提示符会让光标偏出去。

## 10. 调色板与 readline 补全器（484–510 行）

`_palette`（484–500 行）给出"当前光标位置可用的补全列表"，两条规则：

1. `/` 开头且无换行 → 补命令。先查 `self.words`（含动态变体），为空再退回 `SLASH_CATALOG`——注释点明这是为了让自绘编辑器也能 Tab 出 `/skill:name` 这类目录里没有的项。
2. 否则查 `@` token：`at_token(buf, cur)` 拿到光标处的 `@xxx`，交给 `at_files` 回调换候选路径。

`pasting` 时直接返回 `[]`——**粘贴期间不弹菜单**，否则粘贴一段含 `/` 的文本会一直有菜单在闪。

`complete()`（502–510 行）是给 readline 用的同逻辑版本（`state` 递增取下一个候选，上限 20）。它和 `_palette` 是两处重复实现：readline 路径走 `complete`，自绘路径走 `_palette`。这是有意的——两者的接口形状不同（readline 要 `(text, state)` 的拉取式，自绘要一次性拿列表），抽公共函数收益不大。

## 11. 入口分发与页脚高度协商（511–556 行）

`read()`（518–526 行）：双 TTY 才走 `_read_tty()`，否则 `input()`。`_read_tty` 抛 `OSError` 时（比如 fd 突然没了）也降级到 `input()`——**宁可功能退化也不要崩**。

`_footer_rows()`（533–543 行）把 `reserved_bottom` 统一处理成整数：可调用就调（页脚高度随计划行变），是整数就用，都没有就用 `Footer.HEIGHT`。异常时回落到 `Footer.HEIGHT`。这个"可调用或常量"的设计是因为 `LineEditor` 在构造时页脚可能还没 arming，高度得运行时才知道。

## 12. 自绘编辑主循环（557–658 行）

`_read_tty`（557–658 行）是核心。开场 `tty.setraw(fd)` + `\033[?2004h`（开括号粘贴）。

**主循环结构**：

```
while True:
    key = unread.pop(0) or _read_key(fd, timeout=0.12)
    if key is None:
        if on_idle and on_idle(): 重画菜单（wipe=True）
        continue
    ...按键处理...
    selected, palette_rows = self._refresh_palette(...)
```

`timeout=0.12` 让空闲回调每 120ms 有机会跑一次——`on_prompt_idle` 用它来 flush 后台 refine 输出和后台 bash 作业结果（见 [ui/app.md](app.md)）。**没有这个超时，空闲期间后台产生的内容要等用户敲一个键才出现。**

`unread` 列表是"偷看后放回"的缓冲区：Enter 时要看后面有没有字节来区分 Shift+Enter，看到了不是换行的就 `unread.insert(0, nxt)` 塞回去。

**Enter 处理**（593–621 行）是这里最讲究的一段：

```python
if key == "\r":
    nxt = unread.pop(0) if unread else _read_key(fd, timeout=0)
    if nxt is not None and nxt != "\n":
        unread.insert(0, nxt)
more = bool(unread) or _fd_pending(fd, 0.0 if pasting else 0.02)
if not enter_submits(pasting=pasting, more_input=more):
    buf = buf[:cur] + "\n" + buf[cur:]     # 插入换行
    cur += 1
```

**20ms 窗口偷看一眼**：Shift+Enter 在终端里发的是 `\r\n`，两个字节可能不在同一次 `read` 里到达。所以按 `\r` 时用 `timeout=0` 探一下后面有没有东西——有就是换行（Shift+Enter），没有才是真 Enter。注释同时承认了代价：把 Enter 和 Shift+Enter 都发成 `\r` 的终端照常工作，只是用不了 Shift+Enter。

提交时如果当前有菜单，先"取选中项"补进缓冲区再提交——这样 Tab 和 Enter 在菜单打开时行为一致。

提交后 `readline.add_history(buf)`（只在非空时），历史就进了共享的 readline 存储。

**其余按键**（622–656 行）：退格、左右/上下、`home`/`end`、Tab、Ctrl+U。每个编辑键都接受两种形态——`"\x02"`（Ctrl+B）和 `"left"`，`"\x01"`（Ctrl+A）和 `"home"`，`"\x05"`（Ctrl+E）和 `"end"`、`"\x06"`（Ctrl+F）和 `"right"`。方向键序列不是所有终端都发，裸控制码是兜底。

**上下键的三选一**（642–652 行）：

```python
matches = self._palette(buf, cur)
moved = cursor_vert(buf, cur, delta)
if matches:        selected = (selected + delta) % len(matches)   # 菜单里移动
elif moved is not None:  cur = moved                              # 多行：跨行
elif history:      翻历史
```

优先级：有补全菜单就翻菜单，否则多行就跨行，否则翻历史。这个顺序决定了"有多行内容时上下键不能翻历史"——符合直觉（此时用户想的是移动光标）。

Tab（630–639 行）分两种替换：`/` 命令是**整行替换**，`@` token 是**只替换 token**（靠 `replace_at_token`，见 [tools/atfiles.md](../tools/atfiles.md)）。

`finally` 里关括号粘贴 + 恢复 termios。

## 13. 菜单重绘与历史导航（659–723 行）

`_refresh_palette`（659–677 行）重画菜单，返回新的 `(selected, 占用行数)`。`selected >= len(matches)` 时归零——菜单变短（比如从 `/s` 退到 `/`）时选中项不能越界。

`wipe=True` 用于空闲回调触发的重画：此时可能 resize 了，旧菜单占的行数已经不对，所以从 `prompt_row` 一直擦到屏幕底。

`_hist_move`（679–700 行）复刻 readline 的历史语义：**进入历史时记住正在编辑的行**（存进 `original`），一路 `↑` 到底再 `↓` 回来能恢复它。注释点明这是修过一个 bug："以前这里会清掉缓冲区把它弄丢"。

`_history_lines`（702–718 行）优先读 readline 的内存历史（`get_current_history_length` + 逐条 `get_history_item`），失败才读历史文件。优先级反过来会拿不到本进程刚 add 的条目。

`_cursor_row`（720–726 行）包一层：非 TTY 返回 `None`（不查 DSR，理由见第 6 段）。

## 14. 提交与绘制（724–825 行）

`_commit_line`（728–752 行）提交一行时做三件事：

1. **擦掉编辑区**：从 `prompt_row` 到最后一行逐行 `\033[{r};1H\033[2K`；
2. **把最终内容落进对话流**：重画 prompt + 视觉行 + `\r\n`——这样提交的内容进入正常滚动区，成为对话历史的一部分（而不是被后面 `_draw_line` 的擦除逻辑吃掉）；
3. **记录 `last_cursor_row`**：这是给下一个任务初始化流式光标用的。注释点明原因：**任务中途再查 DSR 会和工作线程竞态**，所以在提交这一刻（单线程、无竞态）把位置记下来，`start_task` 直接用它种流光标。

`_draw_line`（754–822 行）画输入行 + 菜单，是文件里最密的一段。几个关键决定：

- **绝对定位不用相对移动**：全部用 `\033[{row};1H` 定位。相对移动会累积误差，而对话流随时可能有别的输出插进来。
- `usable = max(1, cols - 1)`：**最后一列会折行**，而在 DECSTBM 底行折行会滚出第二个菜单（765 行注释）。永远留一列不用。
- **空间不够时的处理顺序**：先裁掉缓冲区顶部的多余正文行（`visual[skip:]`），再在 `last > bottom` 时用 `\033[{deficit}S` 上滚 DECSTBM 区腾位置，最后再按剩余空间截菜单 `matches[:max_menu]`。目的是**不把 prompt 盖到对话内容上**。
- `clear_to = bottom if wipe else min(bottom, prompt_row + max(old_rows, extra))`：非 wipe 时只擦旧菜单占过的行——擦多了会闪，擦少了留残影，`max(old_rows, extra)` 同时覆盖"菜单变长"和"菜单变短"两个方向。
- 收尾用 CUP 把光标放回编辑位置，调 `on_paint`（重绘页脚），flush，然后**缓存 `self._prompt_row`**——下次 `_draw_line` 不用再查 DSR（昂贵且可能吃输入）。

`_save`（824–832 行）是 `atexit` 钩子，把 readline 历史写回 `~/.wheel_history`。包在 try 里的 `OSError`——写历史失败不能阻止退出。

## 15. `pick_list` 方向键选择器（827–880 行）

`/provider`、`/resume`、`/tree`、`/replay` 的选择器。非 TTY 时直接返回初始 `selected`（自动化测试/管道场景下"选第一个"是合理默认）。

`paint` 用 `\033[{n}A` 上移 n 行再逐行重画——**原地覆盖**而不是往上滚，这样选择列表不会在滚动区里堆出几十行。

按键循环里一处值得注意的容错：

```python
try:
    key = _read_key(fd)
except OSError:
    # 选择过程中 tty/管道关闭（窗口关闭、管道断开）：
    # 取消而不是 traceback——dispatch 只捕 KeyboardInterrupt。
    return None
```

`_read_key` 在 fd 消失时抛 `OSError`，而上层 `dispatch` 只捕 `KeyboardInterrupt` 和 `EOFError`——不在这里吞掉就是一条 traceback。

取消键集合是 `{None, "\x03", "\x04", "q", "\x1b", "esc"}`；其余键全部 `continue`（忽略），不做部分匹配跳转。

`finally` 里恢复 termios 也包了 `OSError`——fd 已经没了，此时有意义的是"已取消"这个结果，不是恢复终端。

## 16. `completion_words` 词表拼装（882–897 行）

把 Tab 补全词表拼出来：命令 + `/provider <name>` + `/effort <level>` + `/think <level>` + skill 名。

后两组是**外部数据驱动**的：provider 列表来自 `.env` 里配了 key 的块，effort 档位来自当前 provider 的 `effort_levels`（非推理模型为空列表，所以不会有 `/effort off` 这种无效项），skill 名来自工作区扫描。这个函数在 [ui/app.md](app.md) 的 `_completion_words` 里被包一层，切 provider 后重新 `editor.set_words(...)` 刷新。

硬编码补进去的只有 `/refine auto` 系列和 `/jobs kill`——这几个是 SLASH_CATALOG 里的二级命令，需要"参数已就位"的补全项。

---
