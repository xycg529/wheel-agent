# `ui/app/__init__.py` 逐段讲解

> 本篇讲 TUI 主入口。上游是 `python -m wheel_agent.ui.app`，下游是 [core/loop.py](../../loop-explained.md) 的 `run_agent`（真正的循环）和 ui 各兄弟模块（渲染在 [live.py](app-live.md)、命令在 [commands.py](app-commands.md)、状态在 [state.py](app-state.md)）。

一句话职责：启动横幅、行编辑器接线、忙时 `>` 输入、斜杠命令分发、Ctrl+C 两段式退出——把「用户在终端敲的东西」和「agent 在跑的东西」接起来。

- 行数：774 行
- 依赖：[core/config.py](../core/config.md)、[core/loop.py](../../loop-explained.md)、[core/model.py](../core/model.md)（`make_client`）、[core/queue.py](../core/queue.md)、[core/session.py](../core/session.md)、[core/context.py](../core/context.md)（skill 加载）、[tools/trust.py](../tools/trust.md)、[tools/atfiles.py](../tools/atfiles.md)、[ui/repl.py](repl.md)（行编辑器/按键/选择器）、[ui/app/live.py](app-live.md)、[ui/app/commands.py](app-commands.md)、[ui/app/state.py](app-state.md)、[ui/app/refine.py](app-refine.md)、[ui/graph.py](graph.md)
- 被谁用：`python -m wheel_agent.ui.app`（`ui/app/__main__.py` 调 `main()`）

## 函数速查表

| 名字 | 行号 | 职责 |
|---|---|---|
| `__all__` | 35–48 | 重新导出清单（测试接缝） |
| `HELP` | 89–131 | `/help` 文本（全部命令一览） |
| `_effort_line` / `_effort_choices` | 133–142 | 推理档的单行文本 / 当前 provider 支持的档位 |
| `_completion_words` | 145–151 | Tab 补全词表（命令 + provider + skill + 档位） |
| `ask_yes_no` | 154–160 | y/N 询问：工作线程走队列代理到主线程 |
| `_ask_on_main` | 162–181 | 主线程 y/N：`readline()` 而不是 `input()` |
| `_finish_session` | 183–187 | 任务结束回写会话（turn_offset / usage） |
| `run_task` | 190–251 | 交互模式跑一个任务 |
| `run_json_task` | 254–287 | `--json` 模式：一行 JSON + 退出码 |
| `session` | 290–770 | 主入口：配置、信任、REPL 主循环 |
| `main` | 772–774 | `SystemExit(session())` |

---

## 1. 重新导出与 `__all__`（35–87 行）

```python
__all__ = ["LiveTurn", "STATE", "ToolSnips", "_busy", "_emit", ...]
```

从 `state` / `commands` / `refine` / `live` 四个兄弟模块 re-import 一批名字再统一导出。注释点破原因：**测试 `import wheel_agent.ui.app` 后摸 `app.print_transcript` / `app.ToolSnips`**——把分散在四个模块的名字汇到一个入口，测试和 REPL 分发用同一个接缝。`__all__` 让 pyflakes 知道这些是有意公开的（否则报 unused import）。

## 2. `HELP` 与 effort / 补全 helper（89–151 行）

`HELP`（89–130 行）是 `/help` 的完整文本，也是**全部斜杠命令的权威清单**——读它比读代码快。末尾三行是运行中语义：

> 运行中底部仍有 `>`：回车 = steer，`/follow` 等本轮正常停再投。
> `/stop`、Ctrl+C、Esc = abort。输入只出现在 `>` 里，不会改写 say/think。
> 多行粘贴会收成一条任务，不会把后几行当成 steer。

`_effort_line`（133–138 行）：当前推理档的单行文本，页脚用。`reasoning_payload` 返回 None（非推理模型）时显示 `off`。

`_completion_words`（145–151 行）：Tab 补全词表 = 命令名 + provider 名 + skill 名 + 推理档位。`load_skills(workspace, trusted=trusted)`——**不可信工作区不加载项目 skill**（[core/context.py](../core/context.md)），补全词表也跟着不含它们。

## 3. `ask_yes_no` 与 `_ask_on_main`（154–181 行）

```python
def ask_yes_no(prompt: str) -> bool:
    queue = STATE.active.get("queue")
    if queue is not None and threading.current_thread() is not threading.main_thread():
        return queue.request_ask(prompt)
    return _ask_on_main(prompt)
```

**跨线程 y/N 的关键分叉**：安全门（[tools/safety.py](../tools/safety.md)）在工作线程里执行工具时问 y/N，但**键盘只能主线程读**。工作线程走 `queue.request_ask`（[core/queue.py](../core/queue.md) 的 `AskWaiter` 分段等待），主线程在 `busy_wait` 里轮询 `pending_ask()` 接过来回答。主线程自己问（如 plan 确认在任务开始前）走 `_ask_on_main`。

`_ask_on_main` 用 `sys.stdin.readline()` 而**不是 `input()`**——注释点破：嵌套的 `readline` 会把下一个 `>` 提示藏掉。`EOFError` / `KeyboardInterrupt` 时补 `\n`、重画页脚、返回 False（**默认拒绝**）。

## 4. `_finish_session` 与 `run_task`（183–251 行）

`_finish_session`（183–187 行）：`turn_offset += result.turns`（跨任务连续编号的偏移，[core/loop.py](../../loop-explained.md) 第 5 节）、`usage.add(result.usage)`、`persist(rewrite=True)`。

`run_task`（190–251 行）是交互模式跑一个任务的完整接线：

```python
if not provider_ready(config.provider):
    print(style.red(f"provider {config.provider.name} has no API key. ..."))
    return
STATE.live = LiveTurn()
STATE.active["session"] = session
session.plan.ask = ask_yes_no      # plan 工具的确认走 UI 的 y/N
session.plan.interactive = True
model = make_client(config.provider, effort=config.effort, cache_key=session.cache_key)
if queue is not None:
    model.abort = queue.abort      # 中止信号接入模型调用
model.on_retry = lambda ...: print_event({"type": "api_retry", ...})
```

接线的五件事：

1. **provider 可用性检查**（[core/config.py](../core/config.md) 第 2 节），缺 key 直接红字返回。
2. **plan 的 ask 回调换成 `ask_yes_no`**——plan 提交弹窗走 UI，且 `interactive=True`。
3. **模型客户端带 `session.cache_key`**（前缀缓存）和 `queue.abort`（中止时取消流）。
4. **`on_retry` 回调**：重试时发 `api_retry` 事件，[live.py](app-live.md) 渲染成可见的「重试中」提示。
5. **三个回调**：`on_event=print_event`（事件流→渲染）、`on_delta`（流式增量，**abort 后丢弃后续增量**——`if queue.abort.is_set(): return`）、`on_tool_update`（工具进度，同样 abort 后丢弃）。

```python
result = run_agent(task, workspace, config, model, ask=..., on_event=..., on_delta=...,
                   turn_offset=session.turn_offset, extra_meta={"session_id": ...},
                   queue=queue, session=session, plan=session.plan, runtime_out=STATE.active)
```

**`runtime_out=STATE.active`** 是关键：[core/loop.py](../../loop-explained.md) 第 3 节里 `runtime_out["runtime"] = runtime` 和 `["task_id"]` 会被回填进 `STATE.active`，供 `abort_active()`（下面讲）拿到 runtime 去 `abort_running()`。

收尾：`STATE.live.close()`、`STATE.active["last_task_id"] = result.task_id`、`_finish_session`、刷页脚（`_meter_text` 带 `result.last_usage`）、`maybe_schedule_periodic_refine`（到期的自动 refine，[ui/app/refine.py](app-refine.md)）。

## 5. `run_json_task`（254–287 行）

`--json` 模式：跑一个任务，stdout 只出一行 JSON。

```python
chat = Session.create(workspace)
if not provider_ready(config.provider):
    sys.stdout.write(json.dumps({"error": ..., "stop_reason": "error"}) + "\n")
    return 2
```

缺 key 也**出一行 JSON**（`error` + `stop_reason: error`），退出码 2（配置错误）——机器读 stdout 时不会拿到空。

```python
payload = {"text", "stop_reason", "run_id", "task_id", "session_id", "usage", "changed_files"}
return 0 if result.stop_reason in {"stop", "max_turns", "plan_rejected"} else 1
```

payload 就是 README 里那行 JSON。退出码语义：**正常停 = 0，错误/超轮 = 1，配置错 = 2**（与 README「退出码」一节一致）。注意 `max_turns` 算正常（0）——跑满轮数不是错误。

## 6. `session` 主入口（290–770 行）

这是最大的函数，分六块讲。

### 6.1 启动：参数解析、配置、信任确认（290–314 行）

```python
for arg in argv:
    if arg in {"--json", "-j"}: json_mode = True
    else: cleaned.append(arg)
workspace = Path.cwd()
STATE.auto_refine_every = parse_auto_refine_every()
config = load_config(interactive=not json_mode)
config.runs_dir = (workspace / config.runs_dir).resolve() if not ... else ...
trusted = ensure_project_trust(workspace, interactive=..., ask=...)
```

- **`workspace = Path.cwd()`**：工作区就是当前目录，agent 在哪个目录启动就操作哪个目录。
- **`runs_dir` 相对路径解析到工作区内**：`.wheel_runs` 落在 `<workspace>/.wheel_runs`，replay 跟着工作区走。
- **`ensure_project_trust`**（[tools/trust.py](../tools/trust.md)）：工作区是否可信，未信任且交互时弹 y/N。不可信时项目 skill 不加载（影响 `load_skills` / `expand_skill_command` / 系统提示）。
- `--json` 且无任务：`usage` 到 stderr，退出码 2。

### 6.2 横幅与页脚（316–349 行）

`print_chrome` 打印启动横幅，返回行数（页脚布局要用）。横幅内容：banner 图、`workspace`、`context`（[core/context.py](../core/context.md) 的 `load_project_files` 加载的项目上下文文件相对路径列表）、`provider / model / 上下文窗口`、`effort` 行、`session id`、一行操作提示。

`style.wrap_display(line, cols)` 按终端宽度折行——横幅不超屏。

### 6.3 行编辑器与信号（351–373 行）

```python
Session.purge_empty(workspace)   # 清掉空会话
chat = Session.create(workspace)
editor = LineEditor(
    _completion_words(config, workspace, trusted),
    on_idle=on_prompt_idle,      # 空闲时 flush auto-refine / jobs / resize
    on_paint=STATE.footer.paint,
    at_files=lambda tok: list_at_files(workspace, tok),   # @ 补全
    reserved_bottom=STATE.footer.height,
)
busy_prompt = BusyPrompt(STATE.footer)
prev_winch = signal.signal(signal.SIGWINCH, lambda _: STATE.footer.notify_resize())
```

- **`on_prompt_idle`**（351–354 行）：行编辑器空闲时调，`flush_auto_refine` / `flush_jobs` / `footer.consume_resize()` 任一有产出就返回 True（「有东西要打印」，编辑器据此重画）。
- **`SIGWINCH`**：终端尺寸变化信号，通知页脚重算布局（页脚是固定高度的状态栏，窗口变了要重排）。`prev_winch` 保存旧 handler，退出时恢复（下面 `shutdown_ui`）。
- **`reserved_bottom=STATE.footer.height`**：行编辑器预留底部 N 行给页脚，输入不会画到页脚上。

### 6.4 中止与退出（375–415 行）

```python
def abort_active() -> None:
    queue = STATE.active.get("queue")
    runtime = STATE.active.get("runtime")
    model = STATE.active.get("model")
    if queue: queue.abort.set()          # ① 队列 abort 旗
    if runtime is not None: runtime.abort_running()   # ② 运行时中止在跑的工具
    cancel = getattr(model, "cancel", None)
    if callable(cancel): cancel()        # ③ 模型客户端取消流
```

**三层中止**，对应 [core/loop.py](../../loop-explained.md) 的三处消费点：
- `queue.abort` —— 循环每轮入口/工具批后检查（第 5、11 节）。
- `runtime.abort_running()` —— 中止后台 bash 作业和正在跑的工具（[core/loop.py](../../loop-explained.md) 第 12 节 `except KeyboardInterrupt` 里也调它）。
- `model.cancel()` —— 取消流式 HTTP 连接，让阻塞中的 `complete()` 立刻返回。

`shutdown_ui`（388–396 行）：`abort_active` + `stop_graph_server`（关 `/graph html` 起的 HTTP 服务）+ 恢复 `SIGWINCH` 旧 handler + `footer.disarm()`（解除页脚固定）。**退出前必须做的事全在这一个函数里**，三处退出路径（`/quit`、EOF、二次 Ctrl+C）都调它。

### 6.5 忙时输入：`busy_wait`（417–490 行）

工作线程跑 agent、主线程读键盘，这段是**主线程侧**的忙等：

```python
def busy_wait() -> str | None:
    tty_in = sys.stdin.isatty() and sys.stdout.isatty()
    if not tty_in:
        return _busy_wait_readline()
    import termios
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    prompt = busy_prompt
    try:
        enter_busy_tty(fd)
        sys.stdout.write("\033[?2004h")   # 开 bracketed paste
        while _busy():
            STATE.footer.consume_resize()
            queue = STATE.active.get("queue")
            waiter = queue.pending_ask() if queue is not None else None
            if waiter is not None and not waiter._done.is_set():
                # 工具在工作线程里问 y/N：临时回到主线程问完再继续忙等
                prompt.hide()
                sys.stdout.write("\033[?2004l")
                termios.tcsetattr(fd, termios.TCSADRAIN, old)   # 恢复原始 termios
                waiter.resolve(_ask_on_main(waiter.prompt))
                enter_busy_tty(fd)
                sys.stdout.write("\033[?2004h")
                prompt.show(query_cursor_row(fd))
                continue
            key = _read_key(fd, timeout=0.12)
            ...
```

四个关键点：

1. **非 TTY 走 `_busy_wait_readline`**（466–490 行）：`select.select` 读 stdin，支持 y/N 应答和 steer 行——管道/CI 环境下也能交互（虽然有限）。
2. **y/N 跨线程**：`queue.pending_ask()` 拿到工作线程挂起的 `AskWaiter` 时，**临时恢复原始 termios**（`tcsetattr(TCSADRAIN, old)`）问完 `_ask_on_main`，再 `enter_busy_tty` 切回忙模式。因为 `_ask_on_main` 用 `readline()`，和忙模式的 raw termios 不兼容。
3. **`\033[?2004h/l` 是 bracketed paste 开关**：开启后粘贴的多行文本被终端包成 `[6n ... [~`，行编辑器据此把多行粘合成一条任务（HELP 里「多行粘贴会收成一条任务」的实现）。`finally` 里关掉。
4. **按键分发**：`_read_key(fd, timeout=0.12)` 超时返回 None（继续轮询）；`is_busy_abort_key`（Esc / Ctrl+C / Ctrl+D 空行）→ `raise KeyboardInterrupt`（走两段式退出）；`prompt.feed(key)` 返回非空行 = 一条 steer 或 `/follow` 或 `/stop`，返回给 `dispatch` 处理。

### 6.6 任务启动与分发（492–717 行）

`start_task`（492–519 行）：

```python
def start_task(prompt: str) -> None:
    expanded = expand_skill_command(prompt, workspace, trusted=trusted)
    queue = TurnQueue()
    STATE.active["queue"] = queue
    if sys.stdin.isatty() and sys.stdout.isatty():
        busy_prompt.buf = ""
        busy_prompt.show(editor.last_cursor_row)   # 在任务行下方显示 busy >
    def work() -> None:
        try:
            run_task(config, expanded, workspace, chat, queue=queue)
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            print(style.red(f"agent error: {exc}"))
        finally:
            STATE.active["thread"] = None
            STATE.active["queue"] = None
            STATE.active["runtime"] = None
            STATE.active["model"] = None
    thread = threading.Thread(target=work, name="wheel-run", daemon=True)
    STATE.active["thread"] = thread
    thread.start()
```

- **`expand_skill_command`**：`/skill:name` 注入 skill 全文当任务（[core/context.py](../core/context.md)）。
- **`busy_prompt.show(editor.last_cursor_row)`**：注释点破竞态——busy `>` 必须紧接任务行（`editor.last_cursor_row`），不能在 `show` 里查 DSR（Device Status Report，查光标位置），否则和工作线程写内容竞态。所以**先显示再查**。
- **工作线程 `daemon=True`**：主线程退出时不等工作线程（但 `shutdown_ui` 里 `abort_active` 会先中止它）。
- **`finally` 清句柄**：`thread`/`queue`/`runtime`/`model` 全置 None，下个任务重建——防旧任务的对象被新任务误用。

`dispatch`（521–717 行）：一行输入的分发，**返回 False 表示退出**。结构是先分「忙/闲」，再分「命令/任务」：

```python
if _busy():
    # 忙时：/stop /quit /follow /expand /skill: / 其他 = steer
    if text.startswith("/") and lowered in {"stop","quit","exit","q"}: abort_active(); ...
    if text.lower().startswith("/follow"): queue.follow(payload); ...
    if text.lower().startswith("/expand"): handle_expand(...); ...
    if text.startswith("/") and not text.startswith("/skill:"): 提示; return True
    queue.steer(payload)   # 其余（含普通文本）= steer
    return True
# 闲时：
if not text: return True
if text.startswith("/skill:"): start_task(text); return True
if not text.startswith("/"): start_task(text); return True   # 非 / 开头 = 任务
# / 开头 = 命令，partition(" ") 拆 command + rest，if/elif 链分发
```

忙时的分发和 HELP 末尾三行一一对应：**回车（普通文本）= steer，`/follow` = 排队，`/stop`/Ctrl+C/Esc = abort**。`/follow` 和 `/expand` 在忙时是特例命令（steer 之外的操作），其余 `/` 开头（除 `/skill:`）提示「agent is running」。

闲时命令分发（`/provider`、`/effort`、`/replay`、`/compact`、`/undo`、`/new`、`/sessions`、`/resume`、`/plan`、`/harness`、`/jobs`、`/refine`、`/tree`/`/fork`、`/graph`、`/max-turns`）基本是**参数校验 + 调 [commands.py](app-commands.md) 的 handler + 刷页脚**。几个值得注意的：

- **`/provider`**（无参数弹选择器）：`config.with_provider(rest)` 切换，切完重算补全词表（不同 provider 的 skill/档位可能不同）和页脚。
- **`/effort`**：校验 `want in levels`（当前 provider 支持的档位），`config.with_effort(want)`。
- **`/replay`**：`session` 子命令走 `handle_replay_session`；带 `go` 走 `replay_run`；无 id 弹 `list_run_ids` 选择器。
- **`/new`** / **`/resume`**：换 `chat` 后**必须**同步 `STATE.active["session"]`、刷 plan 页脚和计量页脚——三处漏一处就状态不一致。
- **`/tree` / `/fork`** 都调 `handle_tree`（[commands.py](app-commands.md) 第 3 节），`/fork` 是 `/tree` 的别名。
- **`/follow` / `/stop` 在闲时无意义**：各打印一句 dim 提示，不报错。

### 6.7 主循环（719–770 行）

```python
if argv:   # 命令行带参数：单任务模式
    joined = " ".join(argv)
    if joined.startswith("/"): dispatch(joined)
    else: run_task(config, expand_skill_command(joined, ...), workspace, chat)
    shutdown_ui(); print(); return 0

while True:
    if not _busy(): saw_interrupt = False   # 空闲后重置两段式 Ctrl+C 计数
    try:
        if _busy():
            STATE.footer.arm(); STATE.footer.paint()
            line = busy_wait()
            if line is None:
                flush_auto_refine(config, chat); flush_jobs(); continue
        else:
            flush_auto_refine(config, chat); flush_jobs()
            begin_prompt()
            line = editor.read()
            STATE.footer.arm(); STATE.footer.paint()
    except EOFError:
        shutdown_ui(); print(); return 0
    except KeyboardInterrupt:
        if not keep_after_interrupt(): return 0
        continue
    try:
        if not dispatch(line):
            shutdown_ui(); print(); return 0
    except KeyboardInterrupt:
        if not keep_after_interrupt(): return 0
        continue
    STATE.footer.paint()
```

- **单任务模式**（`argv` 非空）：跑完一个任务就 `shutdown_ui` 退出，不进 REPL 循环。`/` 开头走 `dispatch`（如 `wheel "/help"`），否则同步跑任务。
- **`saw_interrupt` 空闲重置**：`keep_after_interrupt` 的两段式（第一次 Ctrl+C 中止任务、第二次退出）只在「连续忙时」生效，回到空闲就清零——用户在任务结束后按 Ctrl+C 不会累积到退出。
- **忙/闲分流**：`_busy()` 时 `busy_wait()`（忙时 `>`），否则 `editor.read()`（普通行编辑器）。两者都先 `flush_auto_refine` / `flush_jobs`（空闲补发）。
- **EOF / KeyboardInterrupt** 都走 `shutdown_ui` + `keep_after_interrupt`，三处退出路径统一收口。

## 7. `main`（772–774 行）

```python
def main() -> None:
    raise SystemExit(session())
```

`session()` 的返回值（0/1/2）直接变成进程退出码。`python -m wheel_agent.ui.app` 经 `ui/app/__main__.py` 调它。
