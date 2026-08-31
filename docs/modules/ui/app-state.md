# `ui/app/state.py` 逐段讲解

> 本篇讲 TUI 的进程级共享可变状态容器。上游是 `ui/app/__init__.py`、`live.py`、`commands.py`、`refine.py` 四个模块（它们读写同一份状态），下游只有 `ui/style.py`（`Footer`）。

一句话职责：把 TUI 跨模块共享的运行时状态装进一个单例对象 `STATE`，让四个子模块各自能 `from wheel_agent.ui.app.state import STATE` 拿到同一份数据。

- 行数：46 行
- 依赖：[`ui/style.md`](style.md) —— `Footer`（固定页脚的渲染与尺寸管理）
- 依赖：`ui/app/live.py` 的 `LiveTurn`、`ToolSnips`（仅类型注解，`TYPE_CHECKING` 下导入）
- 被谁用：[`ui/app.md`](app.md)（`__init__.py`，读写最全）、[`ui/app-live.md`](app-live.md)、[`ui/app-refine.md`](app-refine.md)、[`ui/app-commands.md`](app-commands.md)

## 目录

- [1. 模块 docstring：为什么抽出来](#1-模块-docstring为什么抽出来-1-5-行)
- [2. 导入与 TYPE_CHECKING 技巧](#2-导入与-type_checking-技巧-7-15-行)
- [3. `AppState.__init__`：七类字段](#3-appstate__init__七类字段-18-42-行)
- [4. `STATE = AppState()`：全局单例](#4-state--appstate全局单例-45-46-行)
- [5. 谁读谁写](#5-谁读谁写场外补充)

---

## 1. 模块 docstring：为什么抽出来（1–5 行）

docstring 交代了来历，也交代了存在理由：

> 应用以前把这些放在模块级全局（`FOOTER`、`LIVE`、`ACTIVE`、`SNIPS`、`AUTO_REFINE_EVERY`、`_refine_*`）。拆成 live/commands/refine 模块后需要一个所有模块都能导入的对象，而不是只在单个模块内有效的 `global` 声明。

两个动机，都和 Python 的 `global` 语义有关：

1. **`global` 是模块级的。** 在 `live.py` 里写 `global FOOTER` 只能重绑 `live.FOOTER` 这个名字；`refine.py` 里 `from live import FOOTER` 拿到的是**当时的值**（对象引用）。一旦 `live.py` 重新 `FOOTER = Footer()`，`refine.py` 持有的还是旧对象，两处看到的页脚不是同一个。改成 `STATE.footer = ...` 是改对象**属性**，所有持有 `STATE` 的模块立刻看到同一个值。
2. **避免循环 import。** `refine.py` 要用 `live.py` 的 `_emit` / `_meter_text`，`live.py` 要用 `refine.py` 的 flush 结果。若共享状态放在任一方，另一方就必须反向 import 这个模块的全局变量，形成环。状态抽到第三方 `state.py` 后，两侧都只依赖 `state.py`，依赖图变成单向：`state.py` ← {`live.py`, `refine.py`, `commands.py`, `__init__.py`}。

`state.py` 因此是 TUI 里唯一**不 import 任何 app 子模块**的文件（只 import `ui/style.py`），这是它能被所有人依赖的前提。

## 2. 导入与 `TYPE_CHECKING` 技巧（7–15 行）

```python
from wheel_agent.ui.style import Footer

if TYPE_CHECKING:
    from wheel_agent.ui.app import LiveTurn, ToolSnips
```

`Footer` 是真导入：`__init__` 里要 `Footer()` 实例化。

`LiveTurn` 和 `ToolSnips` 走 `TYPE_CHECKING` —— 只在类型检查器眼里存在，运行时不 import。这是**打断循环 import 的钥匙**：

- `ui/app/__init__.py` 会 `from wheel_agent.ui.app.live import LiveTurn, ToolSnips`，而 `live.py` 又 `from wheel_agent.ui.app.state import STATE`。
- 若 `state.py` 在运行时 `from wheel_agent.ui.app import LiveTurn`，而 `app/__init__.py` 又先于 `state` 完成初始化去 import `live` → `live` import `state` → `state` import 尚未初始化完的 `app` → 炸 `ImportError`。
- `TYPE_CHECKING` 让运行时这条边消失，注解写成字符串 `"LiveTurn | None"`（配合 `from __future__ import annotations`，整个文件的注解都不求值）。

代价：类型检查器需要 `app` 包能被解析，运行时这两类实际由 `live.py` 提供。

## 3. `AppState.__init__`：七类字段（18–42 行）

```python
class AppState:
    """单例 AppState 的各字段。"""
```

类本身没有方法——纯数据容器。字段按用途分四组，注释就是全部文档：

### 页脚（22 行）

```python
self.footer = Footer()
```

TUI 底部的固定三行（计划状态 / 分隔线 + 工作目录 / token 计量）。为什么放共享状态：流式输出、命令处理、refine 后台线程**都要重绘页脚**，而重绘必须落在同一个 `Footer` 上——两个 `Footer` 实例会各自记住 `_armed`、`_pinned`、`_resized` 状态，抢同一片屏幕行。

`Footer` 自带 `threading.RLock()`，所以从后台线程 `STATE.footer.paint()` 是安全的。

### 流式渲染（23–24 行）

```python
self.live: "LiveTurn | None" = None   # 当前正在流式输出的 LiveTurn（空闲时 None）
```

一个回合的流式状态机（当前开着 say/think/bash 哪个块、已累积的文本）。`__init__.py` 每回合开跑前 `STATE.live = LiveTurn()`，结束时 `STATE.live.close()`。`live.py` 里 `print_event` 用 `STATE.live or LiveTurn()` 兜底——空闲时（比如 `/expand` 打输出）也有对象可调。

### 前台运行句柄（25–33 行）

```python
self.active: dict[str, Any] = {
    "thread": None, "queue": None, "runtime": None, "model": None, "session": None,
}
```

为什么要一个 dict 而不是五个具名字段：这个 dict 直接当 `run_agent(runtime_out=STATE.active)` 的出参容器用（`__init__.py` 243 行）。`run_agent` 往里回填 `runtime` 和 `task_id`，于是 **`/undo-task`、`/jobs` 这类命令不用改签名就能拿到当前运行时的句柄**。副作用是键集合是开放的：`__init__.py` 246 行又写进 `"last_task_id"`，一个不在初始化列表里的键。

各键的读者：

| 键 | 谁写 | 谁读 |
|---|---|---|
| `thread` | `__init__.py` 起任务线程 | `_busy()`（`live.py` 372 行）、`keep_after_interrupt` 的 `thread.join(timeout=2)` |
| `queue` | `__init__.py` | `ask_yes_no`（判断是否在非主线程）、`busy_wait` 取 `pending_ask()`、`_emit` 的中止检查 |
| `runtime` | `run_agent` 回填 | `_sync_plan_footer` 回退取 plan、`/jobs`、`/undo-task` |
| `model` | `__init__.py` | 中止时接 `queue.abort` |
| `session` | `__init__.py` | `_sync_plan_footer`、`flush_auto_refine` 的 current 回退 |

`_busy()` 只读 `thread.is_alive()` —— 这是整个 TUI 判断"前台有没有任务在跑"的唯一依据，忙等循环、`/expand`、refine flush 都问它。

### 工具输出摘存（34–35 行）

```python
self.snips: "ToolSnips | None" = None
```

被裁剪的工具输出仓库，供 `/expand r12` 按 id 取回全文。初始化为 `None`，真正的实例在 `live.py` 62 行模块级创建：`STATE.snips = ToolSnips()`。也就是说 **import `live.py` 才会得到可用的 `snips`**；`live.py` 的 `handle_expand` 和 `print_transcript` 都直接 `STATE.snips.items` / `STATE.snips.add(...)`，不判空——依赖 import 顺序保证它已被填好。

### 自动 refine 三件套（36–42 行）

```python
self.auto_refine_every: int = 8          # 每几个用户轮触发一次（0 = 关）
self.refine_at: dict[str, int] = {}      # 每会话记"上次触发时的用户轮数"
self.refine_lock = threading.Lock()      # 保护下面两个
self.refine_pending: list[dict] = []     # 后台线程产出的待打印 payload
self.refine_thread: threading.Thread | None = None
```

这组字段服务于**后台线程**，是全文件唯一需要加锁的部分：

- `refine_at` 按 `session_id` 存到期计数，所以切换/新建会话后节奏独立，不会互相把计数冲掉。
- `refine_pending` 是后台线程（`target=work`）和主线程（`flush_auto_refine`）之间的队列。后台只 `append`，主线程在空闲时 `list(...) + clear()` 取走一批打印。
- `refine_thread` 兼作"是否已有线程在跑"的标志：`schedule_auto_refine` 持锁检查 `is_alive()`，还在跑就直接跳过这一轮。

只有 `refine_pending` / `refine_thread` 用 `refine_lock` 保护；`auto_refine_every` 和 `refine_at` 只在主线程读写（`/refine auto` 命令和 `maybe_schedule_periodic_refine`），没加锁。

`8` 这个默认值来自 `harness/refine.py` 的 `parse_auto_refine_every()`（环境变量 `WHEEL_AUTO_REFINE`），`__init__.py` 302 行启动时覆盖一次。

## 4. `STATE = AppState()`：全局单例（45–46 行）

```python
# 全局单例。
STATE = AppState()
```

模块级实例，在**第一次 import 时创建并从此不变**——`STATE` 这个名字永不重绑，变的永远是它内部的属性。这正是第 1 节说的"`global` 做不到的事"。

`__init__.py` 把它列进 `__all__` 并 re-export（`"STATE"`，50 行），为的是保持 `wheel_agent.ui.app.STATE` 这个旧路径可用：测试和其他模块可以按老写法 import，不必知道它已经搬到了 `ui/app/state.py`。

## 5. 谁读谁写（场外补充）

按字段统计（`grep -rn "STATE\." ui/`）：

| 字段 | `__init__.py` | `live.py` | `refine.py` | `commands.py` |
|---|---|---|---|---|
| `footer` | 写（set/arm/paint/consume_resize） | 读（height）+ paint | paint + set | paint + set |
| `live` | 写（新建/close/on_delta） | 读 + 写 | — | — |
| `active` | 读 + 写（含 `runtime_out` 回填） | 读（thread/queue/runtime/session） | — | — |
| `snips` | — | 读 + 写 | — | — |
| `refine_*` | 写（`auto_refine_every` 初始值） | — | 读 + 写（全组） | — |

一句话记住分工：**`footer` 人人都能画，`active` 只有主入口写，`refine_*` 只有 refine 模块碰，`snips` 只有 live 模块碰。**
