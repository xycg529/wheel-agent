# `ui/app/commands.py` 逐段讲解

> 本篇讲斜杠命令处理器。上游是 [ui/repl.py](repl.md) 的输入分发（识别 `/` 开头后调这里），下游是 core 层的各功能模块（session / compact / replay / checkpoint / harness）。

一句话职责：每个 handler 都是**薄适配器**——解析参数、做一件事、用共享的 live UI 辅助函数（[live.py](app-live.md)）渲染。从 `wheel_agent.ui.app` 重新导出，让 REPL 分发和测试保持同一个接缝。

- 行数：271 行
- 依赖：[core/session.py](../core/session.md)、[core/compact.py](../core/compact.md)、[core/replay.py](replay.md)、[core/checkpoint.py](../core/checkpoint.md)、[core/events.py](../core/events.md)、[harness/harness.py](../harness/harness.md)、[ui/graph.py](graph.md)、[ui/repl.py](repl.md)（`pick_list` 选择器）、[ui/app/live.py](app-live.md)、[ui/app/state.py](app-state.md)
- 被谁用：[ui/repl.py](repl.md)（`_handle_slash` 的 if/elif 分发链）、`wheel_agent.ui.app.__init__`（重新导出）

## 目录

- [1. `handle_replay_session` 与 `handle_replay`](#1-handle_replay_session-与-handle_replay-31-74-行)
- [2. `handle_resume`](#2-handle_resume77-100-行)
- [3. `handle_tree`：会话树与零拷贝跳转](#3-handle_tree会话树与零拷贝跳转-102-138-行)
- [4. `handle_graph`](#4-handle_graph140-154-行)
- [5. `handle_compact`](#5-handle_compact156-197-行)
- [6. `handle_harness` 与 `handle_jobs` 与 `flush_jobs`](#6-handle_harness-与-handle_jobs-与-flush_jobs199-242-行)
- [7. `handle_undo` 与 `handle_undo_task`](#7-handle_undo-与-handle_undo_task244-271-行)

---

## 1. `handle_replay_session` 与 `handle_replay`（31–74 行）

两个回放入口，对应 session 级和 run 级：

**`handle_replay_session`（31–55 行）**：`/replay session [dest]`。目标目录默认 `.wheel/session-replay/<session_id>`（工作区内，不污染源码），指定 `dest` 时相对路径基于工作区解析。`replay_session`（[ui/replay.py](replay.md) 第 7 节）顺序重放全部 run，每个 run 打印一行 `[status] run_id stop=...`，有 `replay_details` 时附 JSON（`sort_keys=True` 保证稳定输出）。

**`handle_replay`（58–74 行）**：`/replay [id] [go]`。
- `load_run` 找不到时**专门给一条 hint**：「`/replay` 要的是 `.wheel_runs` 的 run id，不是 session id；如果某个 run 记录了 session_id，session id 也能用」。这是把「用户把两种 id 搞混」这个最常见错误直接写进报错。
- 前缀匹配解析后（`bus.run_id != run_id`）打印 `resolved <prefix> → run <full_id>`，让用户知道实际跑的是哪个。
- 无 `go` 只打印时间线（`print_timeline`，纯读不发 API）；带 `go` 时 `replay_run(..., interactive=False)` 真重放一次，打印 `status`（exact/behavioral/drift/error）。

## 2. `handle_resume`（77–100 行）

`/resume [id]`：恢复另一个会话。

- **带 id**：`Session.load_id(workspace, spec)` 直接加载。
- **不带 id**：`Session.list_previews` 列出全部会话，`pick_list` 弹出选择器，**默认选中当前会话**（`sid == current.session_id` 的那行，找不到选第 0 行），用户按回车即取消（`picked is None` 返回原会话）。
- 加载成功重印整个转录（`print_transcript`），让用户确认「我现在在哪」。
- 任何 `FileNotFoundError` 只报错、**返回原会话**——恢复失败不能把当前会话弄丢。

## 3. `handle_tree`：会话树与零拷贝跳转（102–138 行）

`/tree [id]`：列出会话树；带 id（或选择器选中）则跳转/fork。

`_tree_option`（102–106 行）把树的一行渲染成 `*   <id>  <label>`：`*` 标记当前路径上的节点（从根到 leaf），缩进表示深度。

```python
if spec or jumping:
    session.fork(spec or None)   # 跳转 = 移动叶子指针（零拷贝）
    session.persist(rewrite=True)
```

**跳转就是 `fork`**：移动 leaf 指针到新节点，不复制任何数据（[core/session.py](../core/session.md) 第 2 节）。`fork(None)` 不带参数是回退到最近分支点。`persist(rewrite=True)` 因为 leaf 变了、当前对话（根到 leaf 的路径）变了，要重写历史文件。

`jumping` 参数：`/tree` 命令本身传 False（要用户显式选），其他入口（如 fork 后回显树）传 True（已确定要跳）。`KeyError` / `ValueError` 是无效节点 id，报错但不退出（返回 True 表示「命令处理完了」）。

末尾无论是否跳转都重印整棵树（`tree_rows()` 取最新状态），让用户看到「我现在在哪个节点」。

## 4. `handle_graph`（140–154 行）

`/graph [html]`：ASCII 图；带 `html` / `open` / `web` / `serve` 时写文件并起本地 HTTP 服务。

```python
if rest.strip().lower() in {"html", "open", "web", "serve"}:
    path = write_html(graph, workspace)
    url = serve_graphs(path.parent)
    print(...)   # "server stops when you quit wheel"
    return
print(render_ascii(graph), end="")
```

`build_session_graph`（[ui/graph.py](graph.md)）把会话树 + run 事件构成分层图。默认纯 ASCII（终端内直接看）；`html` 模式生成静态 HTML 并用 `serve_graphs` 起一个本地 HTTP 服务（`ThreadingHTTPServer`），**服务随 wheel 进程退出关闭**（注释点破，不留孤儿进程）。空会话直接 `(empty session)` 返回。

## 5. `handle_compact`（156–197 行）

`/compact`：立即压缩当前会话历史（`force=True`，不等自动触发阈值）。

```python
if not provider_ready(config.provider):
    print(style.red("compact needs a provider API key"))
    return
model = make_client(config.provider, effort=config.effort, cache_key=session.cache_key)
```

压缩要**调真模型**生成摘要，所以需要先确认 provider 可用（[core/config.py](../core/config.md) 第 2 节）。`make_client` 带上 `session.cache_key`——压缩的模型调用也走前缀缓存。

```python
try:
    compacted, extra, stats = compact_history(..., force=True, plan_text=...)
except Exception as exc:
    print(style.red(f"compact failed: {exc}"))
    STATE.footer.paint()
    return
```

**宽 catch 保护 TUI**（注释：`/refine` 同样这么保护）——provider 抖动（超时、429）不能搞崩终端。失败时 `STATE.footer.paint()` 重画页脚，把可能残留的半行清掉。

成功路径：`apply_compact` 应用新历史、`usage.add(extra)` 把压缩调用的 token 计进会话总量、`compactions += 1`、`last_compact` 存统计、`persist(rewrite=True)`。注释点破 **`rewrite=True` 的原因**：压缩改了前缀，必须重写历史文件并 bump `cache_epoch`（[core/session.py](../core/session.md) 第 5 节）。

## 6. `handle_harness` 与 `handle_jobs` 与 `flush_jobs`（199–242 行）

**`handle_harness`（199–203 行）**：`/harness` 打印当前 harness（notes + memories）。`format_harness_for_prompt(store.merged(), max_content=None)`——`max_content=None` 表示**不截断**，完整展示（进提示词时才有 240 字符上限，人看要全的）。用 `_emit_clip` 走 live UI 的复制通道。

**`handle_jobs`（206–231 行）**：`/jobs [kill [id]]`。
- 无参数：`format_jobs()` 列出后台 bash 作业。
- `kill` 无 id：弹 `pick_list` 选作业（选项就是 `format_jobs` 的行，取第一列作 id）。
- `kill <id>`：`kill_job(id)`，`ValueError`（id 不存在）报错。
- 其他参数：打印用法。

**`flush_jobs`（233–241 行）**：**不是斜杠命令**，是 REPL 空闲时调用的——把后台作业积压的输出事件打印出来。`drain_job_events()` 非空就逐行 `_emit`（dim 样式，不抢模型输出的视觉权重），刷页脚，返回 True（「这轮空闲有东西输出」，REPL 可据此决定是否再等一轮）。这是后台 bash「异步产出、空闲补发」机制的消费端。

## 7. `handle_undo` 与 `handle_undo_task`（244–271 行）

**`handle_undo`（244–257 行）**：`/undo [n]` 撤销最近 n 个 write/edit（默认 1）。`CheckpointStore.for_workspace(workspace).undo(n)` 返回一组消息（每个文件一行「已还原 x」），逐行绿字打印。无消息 `(nothing to undo)`。

**`handle_undo_task`（260–271 行）**：`/undo-task [id]` 回滚**整个 task** 的文件改动（默认最近一个 task）。`store.rollback_task(task_id or None)`——`None` 指最近 task（[core/loop.py](../../loop-explained.md) 第 3 节的 `runtime.begin_task()` 分配的 id）。打印 `rolled back task <id> (<n> checkpoints)` + 每个文件的还原消息。

两者的粒度差：**`/undo` 按操作数**（撤最近 n 次写），**`/undo-task` 按任务边界**（撤一整个 agent run 内全部写）。用户说「这次任务白做了」用后者，说「最后那个文件写错了」用前者。
