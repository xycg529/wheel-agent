# `ui/app/live.py` 逐段讲解

> 本篇讲 TUI 的输出层（事件 → 终端画面）。上游是 [`core/loop.py`](../../../core/loop.py) 的事件流与 [`ui/app/__init__.py`](app.md) 的回调接线，下游是 [`ui/style.py`](../../../ui/style.py) 的 ANSI 原语。

把 `EventBus` 事件流渲染成终端画面：一个回合的流式块状态机、工具输出裁剪与摘存、转录重印、页脚计量。

- 行数：453 行
- 依赖：
  - [`ui/style.py`](../../../ui/style.py) —— ANSI 颜色、`frame` / `prefix_block`、`Footer`、`writeln_wrapped`、`replace_last_rows`、显示宽度计算
  - [`ui/app/state.py`](app-state.md) —— 进程级单例 `STATE`（页脚、当前 `LiveTurn`、活动任务句柄、snips 仓库）
  - [`ui/markdown.py`](../../../ui/markdown.py) —— `render_markdown`，把模型输出渲染成带样式的文本
  - [`core/meter.py`](../core/meter.md) —— `format_meter` / `compact_count`，页脚计量行
  - [`core/model.py`](../core/model.md) —— `extract_text` / `extract_thinking` / `item_text`（重印转录时从 item 取文本）
  - [`core/compact.py`](../core/compact.md) —— `SUMMARY_MARK` / `is_summary_item`（识别压缩摘要条目）
  - [`core/session.py`](../core/session.md) —— `Session.view_items()` 提供转录
- 被谁用：
  - [`ui/app/__init__.py`](app.md) —— `print_event` 挂成 `on_event`，`LiveTurn.on_delta` / `on_tool_update` 挂成流式回调，`_meter_text` 喂页脚
  - [`ui/app/commands.py`](app-commands.md) —— 复用 `_emit`、`_emit_clip`、`_meter_text`、`print_transcript`（`/tree`、`/resume` 后重印）
  - [`ui/app/refine.py`](app-refine.md) —— 复用 `_busy`、`_emit`、`_emit_clip`、`_meter_text`

## 目录

- [1. 模块定位与导入](#1-模块定位与导入1–20-行)
- [2. 两个裁剪阈值常量](#2-两个裁剪阈值常量22–23-行)
- [3. `ToolSnips`：被裁剪输出的仓库](#3-toolsnips被裁剪输出的仓库26–62-行)
- [4. `LiveTurn` 的状态字段](#4-liveturn-的状态字段66–85-行)
- [5. `Working...` 占位行](#5-working-占位行87–102-行)
- [6. `close` 与 `abandon_say`：两种收口](#6-close-与-abandon_say两种收口104–124-行)
- [7. `_finish_block`：流式行的原地替换](#7-_finish_block流式行的原地替换126–151-行)
- [8. `on_delta` / `on_tool_update`：增量与切块](#8-on_delta--on_tool_update增量与切块153–189-行)
- [9. `_meter_text`：页脚计量行](#9-_meter_text页脚计量行192–204-行)
- [10. `_emit`：唯一的输出口](#10-_emit唯一的输出口207–211-行)
- [11. `parse_tool_args` / `_format_args`](#11-parse_tool_args--_format_args214–244-行)
- [12. `tool_output_label`：三种结果标签](#12-tool_output_label三种结果标签247–258-行)
- [13. `clip_tool_output` / `_emit_clip`](#13-clip_tool_output--_emit_clip261–285-行)
- [14. `print_event`：事件分发主表](#14-print_event事件分发主表288–369-行)
- [15. `_busy` / `_sync_plan_footer`](#15-_busy--_sync_plan_footer372–387-行)
- [16. `print_transcript`：重印整个会话](#16-print_transcript重印整个会话390–441-行)
- [17. `handle_expand`](#17-handle_expand444–453-行)

## 速查表

| 名字 | 行号 | 职责 |
|---|---|---|
| `UI_TOOL_LINES` / `UI_TOOL_CHARS` | 22–23 | 工具输出默认显示上限（6 行 / 500 字符） |
| `ToolSnips` | 26–59 | `/expand` 摘存仓库，环形保留最近 40 条 |
| `LiveTurn` | 66–189 | 一个回合的流式渲染状态机 |
| `_meter_text` | 192–204 | 拼页脚计量行 |
| `_emit` | 207–211 | 唯一的折行输出口 |
| `parse_tool_args` / `_format_args` | 214–244 | 工具参数 → 人类可读多行文本 |
| `tool_output_label` | 247–258 | `blocked` / `error` / `ok` 三态判定 |
| `clip_tool_output` / `_emit_clip` | 261–285 | 裁剪 + 摘存 + 打印工具结果 |
| `print_event` | 288–369 | 事件 → 终端（主分发） |
| `_busy` / `_sync_plan_footer` | 372–387 | 忙状态判定与计划行同步 |
| `print_transcript` | 390–441 | 从会话 items 重印整个转录 |
| `handle_expand` | 444–453 | `/expand` 打印全文 |

---

## 1. 模块定位与导入（1–20 行）

模块 docstring 定下三件事：`LiveTurn` 状态机、工具输出裁剪/摘存、转录回放与页脚计量；并点明**为什么单独成文件**——和命令处理（[commands.py](app-commands.md)）、refine 机制（[refine.py](app-refine.md)）分开，各自好读好测。共享状态不在本模块声明，而是从 [`ui/app/state.py`](app-state.md) 导入 `STATE`——多个模块都要改同一份可变状态，模块级 `global` 只在单个模块内有效。

导入里有一条值得注意的环路处理：`state.py` 用 `TYPE_CHECKING` 从 `wheel_agent.ui.app` 引入 `LiveTurn` / `ToolSnips` 的类型，而 `live.py` 从 `state` 引入 `STATE` 实体——**类型反向、实体正向**，避免运行时循环 import。这也是 `STATE.snips` 在 `live.py`（第 62 行）而不是 `state.py` 里实例化的原因：`ToolSnips` 类定义在 `live.py`，`state` 只持有类型声明。

## 2. 两个裁剪阈值常量（22–23 行）

```python
UI_TOOL_LINES = 6
UI_TOOL_CHARS = 500
```

工具输出默认显示 6 行、500 字符。选小值是因为终端一屏放不下长输出，`read` / `bash` 的结果动辄几百行，全打会把真正的对话推出可视区；超限部分不丢，摘存进 `ToolSnips` 供 `/expand` 取回（第 3 节）。

## 3. `ToolSnips`：被裁剪输出的仓库（26–62 行）

环形仓库，三条设计：

- **id 是 `r1` / `r2` …**，由自增计数器 `_n` 分配，和列表下标解耦——列表裁剪后 id 不会漂移，`/expand r7` 永远指向同一条输出。
- **只留最近 40 条**（`self.items[-40:]`）。40 是终端一屏的量级：再往前翻意义不大，且要防止长会话把输出全文常驻内存。
- **查表是倒序扫**（`reversed(self.items)`），匹配 `rec["n"]` 而非下标，裁剪后依然正确。

`get()` 的参数解析很宽松：空 / `last` / `l` 取最后一条，`r12` 和 `12` 等价，都找不到返回 `None`——`/expand` 无参数就直接看最近一次输出，这是最常用的路径。

第 62 行 `STATE.snips = ToolSnips()` 在模块导入时执行：**snips 是进程级单例**，跨会话、跨任务累积，`print_transcript` 才需要显式清（第 16 节）。

## 4. `LiveTurn` 的状态字段（66–85 行）

```python
self.text / self.thinking / self.bash   # 本回合是否出过 say / think / 工具
self._open: str | None                  # 当前开着的块：text / thinking / bash
self._text / self._think                # 流式累积中的文本
self._waiting                           # 是否已打印 Working... 占位行
```

一个回合内**同时最多开一个块**（`_open` 单值），这就是"流式文本与工具进度同屏"的实现基础：谁先到谁开块，后来者先收口前者再开自己的块（第 8 节）。

`text` / `thinking` / `bash` 是**已完成标志**，`_open` 是**进行中标志**，两者分开是必需的：`message_end` 要判断"这段内容是否已经画过"，避免流式已经画完的块再补一帧（第 14 节）。

`reset()`（79–85 行）先 `close()` 再清标志：turn 边界上可能有未收口的块（上一轮异常中断时），不能留着。

## 5. `Working...` 占位行（87–102 行）

`show_wait()` 在首包到达前打印一行 dim 的 `Working...`：

- **只在 TTY 下打**，且 `_waiting` / 已有开着的块时不重复打。管道模式打占位行只是污染输出。
- 注释说明用 `writeln`（提交完整行）而不是裸写：**让它留在滚动区，而不是落在 `>` 行上**。`>` 固定输入行由 [`ui/repl.py`](repl.md) 的 `BusyPrompt` 管（第 15 节），写错位会把用户正在敲的输入冲掉。

`clear_wait()` 抹掉它时擦的是 **2 行**（`replace_last_rows(2, "", reserved_bottom=STATE.footer.height())`）：`writeln` 把光标留在了下一行，那个空行也要一起擦。

`reserved_bottom=STATE.footer.height()` 是本文件里反复出现的参数：**擦行时不能吃掉页脚的固定行**。页脚高度是动态的（有 plan 行时会变高，见 [`ui/style.py`](../../../ui/style.py) 的 `Footer._height_for`）。

## 6. `close` 与 `abandon_say`：两种收口（104–124 行）

`close()`（104–112 行）按 `_open` 分派：`text` → `finish_say`，`thinking` → `finish_think`，其他（`bash` 或 `None`）→ 只置 `None`。收口前都先 `clear_wait()`。

`abandon_say()`（115–124 行）是**关掉但不重印**：

```python
if self._open in {"text", "thinking", "bash"}:
    style.writeln()
    style.writeln(style.dim("└"))
self._open = None
self._text = ""
self.text = True
```

用在哪：`hide_text` 的内部消息（模型调 `plan` 工具时的正文，[`core/loop.py`](../../../core/loop.py) 第 190 行的 `hide_text`）和 `plan_rejected`。内容已经在别处（计划确认弹窗）展示过了，终端只留一个 `└` 收尾的**空壳块**保持视觉对齐。

关键的一行是 `self.text = True`：把"已出过 say"置真，`message_end` 就不会再补一帧（第 14 节的判断依赖它）。这是一个用状态标志压制重复渲染的技巧，而不是传一个"别打印"的参数下去——因为 `message_end` 可能在完全不同的代码路径（比如非流式到达）触发。

## 7. `_finish_block`：流式行的原地替换（126–151 行）

这是整个文件最核心的渲染动作。开场是 `_open` 里流式写的若干行，收口时要**擦掉它们、在原位画出完整帧**：

```python
if empty:
    if self._open == kind and style.is_tty() and (kind == "text" or buf):
        style.replace_last_rows(style.open_block_rows(buf), "", reserved_bottom=STATE.footer.height())
    self._open = None
else:
    rendered = style.frame(label, render_markdown(body), paint=paint)
    if self._open == kind and style.is_tty():
        style.replace_last_rows(style.open_block_rows(buf), rendered, reserved_bottom=STATE.footer.height())
    else:
        _emit(rendered)
    self._open = None
setattr(self, kind, True)
```

三个设计点：

1. **为什么流式写的是裸文本、收口才渲染 Markdown**：流式阶段拿不到完整文本，Markdown（代码块围栏、表格）无法增量渲染。所以流式只做 `stream_write` 直写，收口时用 `render_markdown` 重画一遍并替换掉裸文本行。代价是内容会"跳变"一次（等代码块围栏闭合后才上色），收益是流式期间零延迟。
2. **`style.open_block_rows(buf)` 算要擦多少行**：header 一行 + body 按 `cols-1` 折行后的行数。`style.py` 的 `writeln_wrapped` 用 `cols - 1` 而不是 `cols` 折行（见 [`ui/style.py`](../../../ui/style.py) 的注释），行数必须按同一口径算，否则会多擦或少擦一行，把相邻块吃掉或留下残影。
3. **`empty` 分支为什么要擦**：推理模型常在 `think` 和工具调用之间吐纯换行的空块。不擦就会在终端留下一串空帧。注意 `kind == "text" or buf` 的条件——空 `say` 也要擦（画了 header），但空 `think` 若从没累积到内容（`buf` 为空）说明块压根没开，不必擦。

`finish_say` / `finish_think`（144–151 行）是两个薄包装，区别只在"什么算空"：`say` 用 `not text.strip()`，`think` 额外把 `…` 和 `...` 也算空——推理模型会先发一个占位符再发真内容。

## 8. `on_delta` / `on_tool_update`：增量与切块（153–189 行）

`on_delta(kind, chunk)` 由 [`ui/app/__init__.py`](app.md) 的 `on_delta` 回调转进来（`run_task` 里挂着 `STATE.live.on_delta`），`kind` 是 `thinking` 或 `text`。

**thinking 分支**（155–166 行）：

```python
if not chunk: return                      # 空增量直接丢：不开空块
self.clear_wait()
if self._open == "text": self.finish_say(self._text)   # 先收口 say 再开 think
if self._open != "thinking":
    style.writeln(style.dim("┌ ") + style.magenta(style.bold("think")))
    self._open = "thinking"
self._think += chunk
style.stream_write(style.crlf(chunk) if "\n" in chunk else chunk)
self.thinking = True
```

**text 分支**（167–179 行）对称，但多一句 `if not style.is_tty(): return`——在 `self._text += chunk` 之后、开块之前返回。非 TTY（管道、`--json`）下**只累积不打印**，等到 `message_end` 统一补一整帧。这样管道输出不会有半截的 `┌ say` 头，也不会有 ANSI 光标序列。

两个分支都要求**切块前先收口前一个块**：这就是同屏的实现。一个回合的典型序列是 `think` 流式 → 收口成 think 帧 → `say` 流式 → 收口成 say 帧 → 工具块 → 下一轮。

`style.crlf(chunk)`：raw 模式（[`ui/repl.py`](repl.md) 的 `enter_busy_tty` 关了 `ICRNL`）下 `\n` 不会自动变 `\r\n`，不转换的话终端会阶梯式右移。

`on_tool_update`（180–189 行）把文字块收口、置 `_open = "bash"`。注意 **`chunk` 参数被 `del` 掉不使用**——签名是为了接 `on_tool_update` 回调，但目前工具进度不发增量，真正的工具反馈是 `tool_execution_start` / `tool_execution_end` 事件（第 14 节）。`_open = "bash"` 的作用是让 `close()` 落进"什么都不做"的分支：工具块不是流式画的，不需要收口。

## 9. `_meter_text`：页脚计量行（192–204 行）

薄包装 [`core/meter.py`](../core/meter.md) 的 `format_meter`，把 provider 的价格和窗口配置喂进去，加上 `session.compactions` 作为压缩次数 `C<n>`。

`last` 参数传的是 `result.last_usage`（最近一次调用的用量）。`format_meter` 内部优先展示 `last`，只有 `last` 全零（首次调用前）才退回 `total`——**页脚显示的是"当前这一轮的输入量"，而不是整个会话的累计量**，因为用户关心的是距离上下文上限还有多远。累计量只在和 `total` 不同时才以 `Σ↑` 补在末尾。

调用点在 [`ui/app/__init__.py`](app.md)：`STATE.footer.set(_meter_text(...))`。

## 10. `_emit`：唯一的输出口（207–211 行）

```python
def _emit(*args, **kwargs) -> None:
    del kwargs
    # 这里不能 STATE.footer.paint()：print 之间 CUP 到最后几行，
    # 会在横线折行时把相邻块粘在一起。
    style.writeln_wrapped(" ".join(str(a) for a in args) if args else "")
```

整篇文件所有"画一个完整块"的动作最终都走这里，**且不重绘页脚**。注释给了原因：`Footer.paint()` 用 CUP（`\033[r;1H`）绝对定位到最后几行画页脚，如果两次 `print` 之间插一次 CUP，光标位置会跳，终端自动换行时相邻两块会粘在一起。

页脚的重绘被推迟到**事件处理完**再统一做（`print_event` 里多处 `STATE.footer.paint()`），或者在 `footer.arm` / `consume_resize` 时做。这是"流式内容走滚动区、页脚走绝对定位"两条通道必须分时的直接后果。

`writeln_wrapped` 按 `cols - 1` 折行：留一列余量，保证终端自己**永不自动换行**（[`ui/style.py`](../../../ui/style.py) 的注释引 pi-tui 的做法）。终端一旦自动换行，页脚预留的行数就会算错。

## 11. `parse_tool_args` / `_format_args`（214–244 行）

`parse_tool_args`（214–226 行）：模型给的 `arguments` 有时是 dict、有时是 JSON 字符串（provider 差异），统一解析成 dict；解析失败就包成 `{"arguments": 原始串}`，**不丢内容**——参数打不出来比格式丑更糟。

`_format_args`（229–244 行）把参数渲染成 `key: value` 多行，两处截断规则：

- `content` / `new_string` / `old_string` 超过 80 字符 → `(123 chars) 前60字符...`。这三个是 `write` / `edit` 的正文，可能几万字符；**先报总长度再看开头**，用户能判断是不是自己要改的那段。
- 其他值超过 120 字符 → 截到 117 加 `...`。

`ls` 的空 `path` 补成 `"."`（233 行）：模型常省略 `path` 表示当前目录，显示成空值会让用户误以为传错了参数。

## 12. `tool_output_label`：三种结果标签（247–258 行）

```python
blocked = bool(item.get("blocked")) or output.startswith("blocked by safety")
error = bool(item.get("is_error")) or output.startswith(
    ("unknown tool:", "PermissionError:", "invalid arguments:")
)
```

除了看事件里的布尔字段，还**回退看输出文本前缀**。原因：转录重印（`print_transcript`）读的是会话 items，item 上不一定带 `blocked` / `is_error` 字段（老会话、或从别的 provider 来的 item），但错误文本前缀是稳定的。这是一处"防御性双判"。

## 13. `clip_tool_output` / `_emit_clip`（261–285 行）

`clip_tool_output`（261–275 行）**先按字符截、再按行截**：

```python
if len(text) > max_chars:
    shown = text[:max_chars].splitlines()
if len(shown) > max_lines:
    shown = shown[:max_lines]
omitted = max(0, total - len(shown))
```

`omitted` 按**原始总行数**算，所以提示是 `… +N lines`（真实省略行数）。字符截断在前是必要的：单行 100KB 的 JSON 不会被"6 行"的判定放过。

`_emit_clip`（278–285 行）有两处值得学：

```python
rec = STATE.snips.add(name, output, turn)   # 先存全文，再裁
body, omitted = clip_tool_output(output)
if omitted:
    body = f"{body}\n/expand {rec['id']}"
```

1. **先摘存后裁剪**，且**无条件摘存**（哪怕没截断）。所以 `/expand` 对任何工具输出都可用，用户不用猜哪条能展开。
2. `/expand r7` 提示**只在真的截断时出现**——没截断时加一行提示是噪音。

## 14. `print_event`：事件分发主表（288–369 行）

`print_event` 是 `on_event` 回调的实体（[`ui/app/__init__.py`](app.md) 的 `run_task` 里 `on_event=print_event`）。一个大的 `if/elif` 链，按 `event["type"]` 分派。

**入口的中止闸门**（290–293 行）：

```python
queue = STATE.active.get("queue")
if queue is not None and queue.abort.is_set() and kind not in {"error", "agent_end"}:
    return   # 已请求中止：除错误/结束外不再渲染
```

用户按 `/stop` 或 `Ctrl+C` 后，工作线程可能还在吐事件——**闸门在 UI 层而不是循环层**做过滤，循环不用知道有人在看。留 `error` 和 `agent_end` 通过：中止的原因（"interrupted"）必须显示出来，否则用户不知道发生了什么。

`live = STATE.live or LiveTurn()`：空闲时（比如 `refine` 后台线程发事件）没有活动回合，临时建一个一次性对象兜底，避免 `None` 判断散落各处。

各分支：

| 事件 | 行号 | 渲染动作 |
|---|---|---|
| `turn_start` | 295–303 | `live.reset()`；`user=True` 时打空行 + `── turn N ──`（用 `display_turn`，跨任务连续的用户轮编号） |
| `message_start` | 304–306 | 绑定 `STATE.live`，打 `Working...` 占位 |
| `message_end` | 307–321 | 收口/补帧（见下） |
| `plan_rejected` | 322–328 | `abandon_say` + 黄字提示 + `_sync_plan_footer(busy=False)` |
| `tool_execution_start` | 329–335 | 收口文字块，打 `tool  <name>` 黄框（参数已由 [`tools/audit.py`](../tools/audit.md) 脱敏） |
| `tool_execution_end` | 336–350 | `ok`/`error`/`blocked` 三色框 + 裁剪输出；`plan` 工具额外同步页脚计划行 |
| `error` | 351–357 | 红字；`transient` 时补一行 dim 提示"会话保留，可重发" |
| `api_retry` | 358–362 | dim 的 `retry N — 原因`，然后**重新打 `Working...`**（重试期间没内容，占位要回来） |
| `compact` | 363–369 | 只在 `did` 为真时提示：`条目数 → 条目数 / token → token / epoch N` |

`message_end` 分支是唯一有分支逻辑的（307–321 行）：

```python
if event.get("hide_text"):
    live.abandon_say()                       # 内部消息：不显示
elif live._open == "text":
    live.finish_say(text)                    # 流式开着的块：用完整帧替换流式行
elif live._open == "thinking":
    live.finish_think(thinking or live._think)
else:
    live.close()
if thinking and not live.thinking and not hide_text:
    _emit(style.frame("think", ...))         # 非流式到达的 think 补帧
if text and not live.text and not hide_text:
    _emit(style.frame("say", ...))           # 非流式到达的 say 补帧
```

两个 `if` 补帧是给**非流式 provider** 用的：没有 `on_delta` 时，`_open` 一直是 `None`，`live.thinking` / `live.text` 为假，这里补画完整帧。有流式时标志已为真，不会重复画。

注意 `live._open` 是私有字段却被外部直接读——状态机和事件分发是强耦合的一对，改 `LiveTurn` 内部表示必须同步改这里。

`api_retry` 事件不在 [`core/loop.py`](../../../core/loop.py) 里发，而是模型客户端重试时通过 `model.on_retry` 回调直接调 `print_event`（[`ui/app/__init__.py`](app.md) 第 216 行接的线）——**重试发生在模型客户端内部，循环看不到**。

## 15. `_busy` / `_sync_plan_footer`（372–387 行）

`_busy()`（372–375 行）：看 `STATE.active["thread"]` 是否还活着。这是"当前有没有任务在跑"的唯一判定来源，被 `busy_wait`、`refine` 等复用。

`_sync_plan_footer()`（378–387 行）：把计划状态推进页脚。两段查找——先从 session 拿 `plan`，拿不到再从 `runtime` 拿（任务运行时 `runtime.plan` 才是活的）。`busy` 参数是显式覆盖口：`plan_rejected` 时传 `busy=False`，让 [`PlanStore.footer_lines`](../core/plan.md) 在全部完成且空闲时**返回空列表自动隐藏计划行**，不白占屏幕。

页脚的绘制本身在 [`ui/style.py`](../../../ui/style.py) 的 `Footer`（`set_plan` → `paint`），本模块只负责"算出该显示什么"。

分工要点：**`>` 固定输入行由 [`ui/repl.py`](repl.md) 的 `BusyPrompt` 管**（`footer.set_input`），页脚的计划行和计量行由本模块管（`footer.set_plan` / `footer.set`），底层 ANSI 序列全在 `style.py`。三层各管一段，中间没有跨层直接写转义序列。

## 16. `print_transcript`：重印整个会话（390–441 行）

`/tree` 跳转、`/resume` 恢复后要让用户看到上下文，就得把会话 items 重画一遍。

开头清 snips（396–398 行）：

```python
STATE.snips.items.clear()
STATE.snips._n = 0
```

**连私有计数器一起重置**——重印后编号从 `r1` 重新开始，和屏幕上看到的第一条工具输出对齐。代价是跳转前记下的 `/expand r7` 会指向别的内容（见第 18 节）。

用一个 `calls` dict 做 `call_id → 工具名` 的映射（400 行）：会话 items 里 `function_call` 和 `function_call_output` 是两条独立记录，靠 `call_id` 配对；`pop` 出来用完即弃，避免 dict 随会话增长。

各 item 类型的渲染：

- `reasoning` / `thinking` → `extract_thinking` 取文本，画 magenta `think` 帧
- `role == "user"` → 压缩摘要（`is_summary_item`）画成 dim 的 `summary` 块（剥掉 `SUMMARY_MARK`），否则画 `── turn N ──` + cyan `you` 块
- `function_call` → 记下 `call_id`，画黄 `tool` 块
- `function_call_output` → `pop` 出工具名，用 `tool_output_label` 判三态，`_emit_clip` 画（因此重印的输出同样可 `/expand`）
- 其他 → 先试 `extract_thinking`，再试 `extract_text`，`role == "assistant"` 时退回 `item_text`

最后的 `else` 分支兜三种 provider 的 item 形态（Responses 的 `output_text`、Chat 的 `content`、老格式的 assistant 消息），这也是为什么 [`core/model.py`](../core/model.md) 要提供三个提取函数。

结尾 `STATE.footer.paint()`：重印了一整屏内容，页脚要重画一次确保还贴在最底部。

## 17. `handle_expand`（444–453 行）

`/expand` 的实现。`ToolSnips.get()` 找不到时给可执行的提示（"try /expand r7" 或 "no tool output yet"），而不是干巴巴一句 "not found"——**提示里带上当前可用的 id，用户照抄即可**。

找到就用 cyan 的 `full  r7  bash` 框画全文，不裁剪。
