# `ui/app/refine.py` 逐段讲解

> 本篇讲 `/refine` 命令的 UI 层。上游是 [`ui/app/__init__.py`](app.md) 的命令分发和空闲循环，下游是 [`harness/refine.py`](../harness/refine.md) 的提炼流程与 [`ui/app/live.py`](app-live.md) 的展示原语。

把「从会话轨迹提取持久经验」这件事接进终端：手动 `/refine` 在 TUI 线程同步跑并内联报结果，自动 refine 在后台线程跑完排队，等下一个空闲提示时统一打印。

- 行数：206 行
- 依赖：
  - [harness/refine.py](../harness/refine.md) —— 底层提炼流程（`run_refine` / `format_refine_result` / `parse_refine_args` / `refine_due`）
  - [harness/harness.py](../harness/harness.md) —— `HarnessStore` 的加载与写入
  - [ui/app/live.py](app-live.md) —— `_busy` / `_emit` / `_emit_clip` / `_meter_text` 四个展示原语
  - [ui/app/state.py](app-state.md) —— `STATE` 单例（节奏、待处理 payload、工作线程）
  - [ui/style.py](../../../ui/style.py) —— `prefix_block` / 上色器 / `writeln_wrapped`
  - [core/model.py](../core/model.md) —— `make_client` 造一个专用模型客户端
  - [core/config.py](../core/config.md) —— `provider_ready` 判断有没有 key
  - [core/session.py](../core/session.md) —— `Session.items` / `cache_key` / `invalidate_cache`
- 被谁用：
  - [ui/app/\_\_init\_\_.py](app.md) —— 每轮结束后调 `maybe_schedule_periodic_refine`；行编辑器空闲时调 `flush_auto_refine`；`/refine` 命令分发到 `handle_refine` / `handle_refine_auto`
  - [ui/app/commands.py](app-commands.md) —— `/harness` 复用 `_harness_store`

## 目录

- [1. 模块 docstring：两条入口，一个执行核心](#1-模块-docstring两条入口一个执行核心1–29-行)
- [2. `_harness_store`：拿到工作区的存储](#2-_harness_store拿到工作区的存储31–38-行)
- [3. `_execute_refine`：唯一的执行核心](#3-_execute_refine唯一的执行核心40–64-行)
- [4. `maybe_schedule_periodic_refine`：到期判定](#4-maybe_schedule_periodic_refine到期判定67–75-行)
- [5. `schedule_auto_refine`：后台线程 + 快照](#5-schedule_auto_refine后台线程--快照77–112-行)
- [6. `flush_auto_refine`：空闲时统一打印](#6-flush_auto_refine空闲时统一打印115–136-行)
- [7. `handle_refine_auto`：改节奏](#7-handle_refine_auto改节奏139–165-行)
- [8. `handle_refine`：手动入口](#8-handle_refine手动入口167–206-行)

## 1. 模块 docstring：两条入口，一个执行核心（1–29 行）

docstring 直接画出模块的两条路径：

- **manual** —— `/refine [instructions] [--global] [--rollback <id>]`，在 TUI 线程**同步**执行，内联报 `ok` / `partial` / `error`；
- **auto** —— 每 N 个用户轮由**后台线程** refine 会话快照并排队一个 payload，`flush_auto_refine` 在下一个空闲提示时打印它。

设计意图：refine 是一次模型调用（几百毫秒到几秒），同步跑会卡住输入行，所以自动路径必须走后台；但手动 `/refine` 用户就盯着结果，同步反而更符合直觉——敲完命令立刻看到提取了什么。两条路径的差异只在「谁调用 `_execute_refine`、结果怎么呈现」，构造模型调用和 store 的代码只有一份。

共享可变状态三件套都在 `STATE`（[ui/app/state.py](app-state.md)）：`auto_refine_every`（节奏）、`refine_at`（每会话上次触发的轮数）、`refine_pending` + `refine_thread`（待处理 payload 和工作线程）。拆模块后不能再用模块级 `global`，所以集中到一个单例对象。

## 2. `_harness_store`：拿到工作区的存储（31–38 行）

```python
return HarnessStore.for_workspace(
    workspace,
    session_path=session.path,
    interactive=True,
)
```

一句话函数，但有两个关键参数：

- **`session_path=session.path`** —— local 作用域的 harness 文件是**按会话路径**存的（见 [harness/harness.py](../harness/harness.md) 的 `local_harness_path`）。所以 `/refine` 默认写 local，实际是写「当前会话自己的笔记」，不会漏到别的会话。
- **`interactive=True`** —— `HarnessStore.target()` 里 global 写入只允许交互模式，这里恒为 True（UI 层一定是交互的）。后台 auto 路径同样传 True，但它固定 `global_=False`，不碰 global。

这个函数也被 [ui/app/commands.py](app-commands.md) 的 `/harness` 复用——打印和提炼必须看到同一个视图。

## 3. `_execute_refine`：唯一的执行核心（40–64 行）

```python
model = make_client(config.provider, effort="off", cache_key=cache_key)
store = _harness_store(workspace, session)
return run_refine(store, items, model, instructions=..., rollback_id=..., global_=global_)
```

三个设计点：

1. **`effort="off"`** —— refine 是后勤活，只要提取不要思考。docstring 明写「推理档固定 off」。省推理 token、也省延迟。（底层 `_complete_json` 还会再保险一次：临时把 `model.effort` 设成 `"off"`，调完恢复，因为模型客户端是和主循环共享的同一个对象。）
2. **`make_client` 新建客户端而不是复用主循环的** —— 主循环那个客户端的 `effort` 跟着会话档位走，且可能正在流式输出。新建一个是干净隔离。
3. **`cache_key=cache_key` 用会话的** —— 让这次提炼调用能搭上会话的前缀缓存。

**为什么 `items` 是参数**：手动路径传 `session.items`（活的列表），自动路径传**快照拷贝**。这个差别是第 5 节的核心。

返回值是 `run_refine` 的 `(result, extra_usage)` 对。**本函数不做任何呈现**——调用方决定内联标签还是排队 payload。这是两条路径能共享一个核心的前提。

## 4. `maybe_schedule_periodic_refine`：到期判定（67–75 行）

```python
n = session.user_turns()
last = STATE.refine_at.get(session.session_id, 0)
if not refine_due(n, STATE.auto_refine_every, last):
    return
STATE.refine_at[session.session_id] = n
schedule_auto_refine(config, workspace, session)
```

调用点在 [ui/app/\_\_init\_\_.py](app.md) 的 `run_turn` 末尾（251 行）——**每个任务跑完后**检查一次，且只在 `config.interactive` 时。

判定逻辑委托给 `refine_due`（[harness/refine.py](../harness/refine.md)）：`every <= 0` 或 `user_turns < every` 直接 False，否则 `user_turns - last_at >= every`。用「用户轮数」而不是「任务数」或时间，是因为经验积累的粒度是人和 agent 的交互轮次。

`STATE.refine_at` 是**按 `session_id` 记的字典**——多会话（`/tree` 跳转、`/resume`）时各自的节奏独立，不会互相干扰。注意 `refine_at` 的写入没有加锁：这个函数只在 TUI 线程调用，写的是自己的 key，不存在竞争。

**先记账再触发**（`refine_at[...] = n` 在 `schedule_auto_refine` 之前）：即使后台线程跑失败或跳过，节奏也不会被反复重试打乱。

## 5. `schedule_auto_refine`：后台线程 + 快照（77–112 行）

**并发节流**（80–84 行）：

```python
with STATE.refine_lock:
    if STATE.refine_thread is not None and STATE.refine_thread.is_alive():
        return   # 上一次还没跑完：跳过
```

不排队、不等待——上一次没跑完就直接放弃这一次。设计取舍：自动 refine 是「有则更好」的优化，为了它堆积线程或阻塞 TUI 不值当。

**会话快照**（85–86 行）：

```python
items = [dict(item) for item in session.items]   # 快照：后台读拷贝，不锁会话
cache_key = session.cache_key
```

这是整个后台路径最关键的一行。浅拷贝每个 item（`dict(item)`），后台线程读的是冻结的列表，而 TUI 线程可能同时在往 `session.items` 里追加新消息。**不锁会话**的代价是：提炼看的是拍照那一刻的轨迹，可能漏掉用户刚敲的那一句。换来的是零锁竞争——主循环永远不会被后台 refine 卡住。（item 内部的嵌套结构仍是共享引用，但 agent 循环只 append 不改已有 item，所以安全。）

**`work()` 闭包**（88–110 行）：

```python
result, extra = _execute_refine(
    config, workspace, session, items,
    cache_key=cache_key,
    instructions=(
        "Periodic refine after several user turns. "
        "Extract only durable lessons. Skip one-off task progress."
    ),
    global_=False,
)
```

`instructions` 是硬编码的——自动路径没有用户指令，这句提示词承担了「只提取持久教训、跳过一次性任务进度」的约束。**`global_=False` 恒成立**：后台线程无人值守，绝不能写全局 harness（[harness/harness.py](../harness/harness.md) 的 `target()` 也会拦一道，但这里根本不尝试）。

**payload 归一化**（99–109 行）：成功就排一个带 `session` / `usage` / `text` / `applied` 的 dict，**异常也排一个 payload**（`{"session": ..., "error": str(exc)}`）。这样后台的失败不会丢——它会和普通结果一起在下次 flush 时以 `error  refine` 块打出来。宽 `except Exception` 是刻意的：后台线程炸了不能带走 TUI。

`applied` 字段在排队时就预先算好（`bool(applied)`），flush 时只负责挑颜色，不再解析结果结构。

**线程是 daemon**（111–112 行）：`daemon=True, name="wheel-refine"`，退出时不等它。

## 6. `flush_auto_refine`：空闲时统一打印（115–136 行）

```python
if _busy():
    return False
```

**第一道闸**：前台任务在跑就一个字都不打。原因很实在——流式输出正在刷屏，插一段 refine 结果会把 transcript 冲乱。

**批量取走**（119–122 行）：

```python
with STATE.refine_lock:
    batch = list(STATE.refine_pending)
    STATE.refine_pending.clear()
```

在锁里一次性把列表换空，锁外慢慢打印。这样后台线程追加新 payload 不会被打印过程阻塞。

**逐条处理**（124–134 行）：

```python
target = item.get("session") or current
if item.get("error"):
    _emit(style.prefix_block("error  refine", str(item["error"]), style.red))
    continue
target.usage.add(item["usage"])
target.invalidate_cache()   # harness 变了：下个任务重建上下文
label, paint = ("ok", style.green) if item.get("applied") else ("skip", style.dim)
_emit_clip("refine", label, item["text"], paint)
if target is current:
    STATE.footer.set(_meter_text(config, current))
```

- **按 payload 里记的 session 归属**，不是一律用 `current`。用户在后台 refine 期间可能已经 `/tree` 跳到别的会话了，token 必须记到正确的会话头上。
- **`target.usage.add`** —— 后台模型调用花的 token 要计入会话账，否则计量表会漏。
- **`target.invalidate_cache()`** —— harness 笔记变了，系统提示内容就变了。自增 `cache_epoch` 并全量重写会话文件，强制下次模型调用用新 cache key、重建上下文（见 [core/session.py](../core/session.md)）。**这一步是正确性的关键**：不失效缓存，下一个任务还会用装着旧笔记的旧前缀。
- **两种结果色**：有条目应用成功 → 绿色 `ok`；模型判断「没什么可提取的」（空 edits）→ 暗色 `skip`。注意 `skip` 不是错误。
- **`_emit_clip`** 会把全文摘存进 `STATE.snips` 并截断显示（默认 6 行 / 500 字符），超出部分提示 `/expand <id>`。refine 结果复用工具输出的同一套展示/展开机制。
- 只有 `target is current` 时才刷新页脚计量——别的会话的用量不该动当前页脚。

最后 `STATE.footer.paint()` 重画固定行，返回 `True` 告诉调用方「有东西打印了」。

**调用点**（[ui/app/\_\_init\_\_.py](app.md)）：`on_prompt_idle()`（353 行）里和 `flush_jobs()` 并列；主循环里空闲分支（746 行）和 busy 等待返回 `None` 时（742 行）各调一次。`on_prompt_idle` 的返回值参与「是否需要重画输入行」的判断。

## 7. `handle_refine_auto`：改节奏（139–165 行）

纯状态命令，只读写 `STATE.auto_refine_every`，不发任何模型调用。

参数解析的宽松度是刻意的：`off` / `0` / `false` / `no` 都认，`on` / `true` / `yes` 等价于 **8**（和 `parse_auto_refine_every()` 的 `WHEEL_AUTO_REFINE` 环境变量默认值一致）。

```python
STATE.auto_refine_every = max(0, int(spec))
```

`max(0, ...)` 把负数夹成 0（关闭），避免「每 -3 轮」这种无意义配置让 `refine_due` 恒真。解析失败只打 `usage:` 提示行，不抛异常——`/` 命令处理器的通用约定。

注意这里**只改内存里的值**，不写配置文件：重启后回到 `WHEEL_AUTO_REFINE` 或默认 8。

## 8. `handle_refine`：手动入口（167–206 行）

**参数解析与前置检查**（169–178 行）：

```python
try:
    options = parse_refine_args(rest)
except ValueError as exc:
    print(style.red(str(exc)))
    return
if not session.items and not options.get("rollback_id"):
    print(style.dim("nothing to refine"))
    return
if not provider_ready(config.provider):
    print(style.red("refine needs a provider API key"))
    return
```

- `parse_refine_args` 在 [harness/refine.py](../harness/refine.md)，负责认 `--global` 前缀、`rollback <id>`、以及普通指令文本；`usage` 错误由它抛 `ValueError`，这里转成红色一行。
- **`not session.items` 但允许 rollback** —— 空会话没东西可提炼，但回滚是查历史记录，不需要当前轨迹（而且换会话后 `/refine rollback <id>` 照样能用）。
- **`provider_ready`** 统一判断「有 key 还是本地免鉴权端点」，避免和配置层标准漂移。

**同步执行**（179–191 行）：直接 `_execute_refine`，传活的 `session.items`（不是快照——TUI 线程自己跑，没有并发风险）。异常走 `prefix_block("error  refine", ...)` + `STATE.footer.paint()`：和后台路径的错误用**同一个标签格式**，用户看到的样子一致。

**记账与缓存失效**（192–193 行）：和 flush 路径完全相同的两步——`session.usage.add(extra)`，然后 `session.invalidate_cache()`。

**三态标签**（194–201 行）：

```python
applied = [row for row in result.get("appliedEdits") or [] if row.get("applied")]
failed  = [row for row in result.get("appliedEdits") or [] if not row.get("applied")]
if failed and not applied:
    label, paint = "error", style.red
elif failed:
    label, paint = "partial", style.yellow
else:
    label, paint = "ok", style.green
```

自动路径只有 `ok` / `skip` 两态（不区分部分失败），手动路径多一个 **`partial`**（黄色）：有成功也有失败。这是 [harness/harness.py](../harness/harness.md) `apply_proposal` 的乐观并发检查在 UI 上的投影——某条目在规划期间被改过，那条 edit 被拒（`entry changed during refinement planning`），其余照常应用。**部分失败对用户是有意义的信息**，所以单独给一档颜色。

最后 `_emit_clip("refine", label, format_refine_result(result), paint)` 打出结果块，并 `STATE.footer.set(_meter_text(config, session))` 更新页脚。

**关于「逐条确认」**：这一版**没有交互式审核 UI**。不是逐条确认，也不是批量确认——流程是「先应用，再展示」：`run_refine` 内部规划完直接 `apply_proposal` 落盘并 `store.record()` 记进历史，`handle_refine` 拿到的 `result` 已经是既成事实，UI 只负责把它格式化成人能读的文本。

撤销靠**事后回滚**而不是事前确认：`format_refine_result` 打印的每行都带 `refinement_id`（如 `local r7k2m3ab: ...`），用户看不惯就 `/refine rollback <id>`（[harness/refine.py](../harness/refine.md) 的 `rollback_proposal` 会构造反向提案）。取舍很清楚：提炼结果通常 2–5 条、改动很小，为了它加一层逐条 y/N 弹窗，打断成本比回滚成本高。
