# loop.py 逐段解释

`core/loop.py`（472 行）是整个 agent 的心脏：一个 ReAct 循环——
**推理（reason）→ 调工具（act）→ 看结果（observe）**，重复直到模型不再调工具。

文件里有 5 个函数：

| 函数 | 行号 | 职责 |
|---|---|---|
| `run_agent` | 44–375 | 主入口，包含整个循环和收尾 |
| `_push` | 378–386 | 往上下文追加一条消息（并同步进会话树） |
| `_sync_cache_key` | 389–393 | 会话缓存纪元变化时，同步 prompt cache key |
| `_complete_with_overflow` | 395–434 | 调一次模型；上下文溢出时强制紧凑后重试一次 |
| `_run_tools` | 437–472 | 执行一批工具调用，前后拍工作区快照、发审计事件 |

循环里有两条平行的主线：

- **上下文 `items`**：喂给模型、决定了模型能看到什么（第 3、9 节）；
- **事件流 `EventBus`**：写盘给 UI、审计和 replay 用（第 19 节）。

---

## 1. 导入与回调签名（1–38 行）

导入按职责分四组：

- **`tools.audit`**：审计工具。`workspace_manifest`（工作区文件清单快照）、`workspace_fingerprint`（快照指纹）、`workspace_changes`（两次快照的 diff）、`redact_tool_args` / `redact_tool_output`（日志脱敏）、`tool_audit`（一次调用的审计记录）、`item_audit`（上下文指纹）、`environment_fingerprint`（运行环境指纹）。
- **`core.compact`**：`compact_history`（把旧历史压成摘要）和 `is_context_overflow`（判断异常是否是各 provider 措辞各异的"上下文超长"错误）。
- **`core` 核心件**：`AgentConfig`（配置）、`EventBus`（事件总线）、`ModelClient`（模型封装）、`PlanStore`（计划工具的状态）、`TurnQueue`（运行中用户输入队列）、`SafetyGate`（安全门）、`Session`（会话持久化）、`Workspace`（工作区封装）、`ephemeral_items` / `system_prompt`（系统提示拼装）。
- **`tools.tools`**：`ToolRuntime`（工具执行器）、`parse_function_calls`（从模型输出里解析工具调用）、`tool_schemas`（工具 JSON schema，喂给模型）。

三个回调签名（都是"函数类型别名"，方便 UI 层传函数进来）：

```python
Printer      = Callable[[str], None]                    # 打印一行
DeltaFn      = Callable[[str, str], None]               # 流式增量（文本, 思考标记）
ToolUpdateFn = Callable[[str], None]                    # 工具执行进度
```

---

## 2. `run_agent` 签名（44–74 行）

```python
def run_agent(task, workspace, config, model, *, bus, safety, ask,
              on_event, on_delta, on_tool_update, extra_meta, items,
              turn_offset, compact, queue, session, plan, runtime_out) -> RunResult
```

参数分五类：

1. **必需三件套**：`task`（用户任务文本）、`workspace`（工作区目录或 `Workspace`）、`config` + `model`（配置和模型客户端）。
2. **注入钩子**（都可选，不传用默认）：`bus` 事件总线、`safety` 安全门、`ask` y/N 询问回调、`on_event` / `on_delta` / `on_tool_update` 显示回调。
3. **运行状态续接**：`session`（多轮会话）、`plan`（计划状态）、`turn_offset`（跨任务连续编号的偏移）、`items`（无 session 时的裸上下文）、`compact`（紧凑开关）。
4. **交互通道**：`queue`（`TurnQueue`：steer/follow/abort 三条通道）。
5. **出参旁路**：`runtime_out`（回填 `runtime` 和 `task_id`，供外部拿来做 `/undo-task` 等）。

docstring 一句话概括主循环：**每步调模型 → 无工具调用则收场（或消化排队输入），有则执行工具、结果追加进上下文继续。**

---

## 3. 初始化：组件组装（76–113 行）

循环开始前的"开机"阶段，每个组件一行：

```python
ws = Workspace(workspace)                 # 统一成 Workspace 对象
bus = bus or EventBus.create(config.runs_dir)   # 没有就建一个，事件落盘
if on_event: bus.subscribe(on_event)      # 显示回调挂到总线上
```

总线是贯穿整个循环的第二条主线（和上下文 `items` 平行），作用见第 19 节。


**默认安全门**（78–80 行）：没传 `safety` 时新建一个，并把 `session.approvals`（上一轮用户批准过的 bash 前缀）载入 `memory`——跨轮免重复确认。

**默认计划存储**（81 行）：没传 `plan` 时用 session 的，否则新建 `PlanStore`。

**Harness 与工具运行时**（82–86 行）：

```python
store = HarnessStore.for_workspace(...)   # 持续学习笔记（prompt/memory 两层作用域）
runtime = ToolRuntime(ws, safety, on_update=on_tool_update, plan=plan, harness=store)
task_id = runtime.begin_task()            # 开一个 checkpoint 任务，/undo-task 用它
```

**初始指纹**（87–88 行）：对当前工作区拍 `manifest`（文件清单）和 `fingerprint`（指纹）。这是"改了什么"对比的基准。

**工具 schema 与系统提示**（93–100 行）：

```python
tools = tool_schemas(list(runtime.tools.values()))
trusted = is_trusted(ws.root) or not project_skill_dirs(ws.root)
instructions = system_prompt(ws.root, trusted=trusted, harness=store.merged())
extra_input = ephemeral_items(ws.root, plan)
```

- `trusted`：工作区可信（或根本没有项目级 skill 目录）才把项目 skill 注入系统提示——防止不可信仓库的 skill 文件劫持提示词。
- `extra_input`：**本轮临时上下文**（当前日期、计划状态等），每轮重新生成、追加在 `items` 后面发，但**不进历史**（所以下面每轮都会刷新它）。

**任务入队**（101–103 行）：有 session 时 `items` 直接是 `session.items` 的别名（改一处两处都变）；然后 `_push` 第一条 user 消息（任务本身）。

---

## 4. 状态变量与 `agent_start` 事件（104–128 行）

```python
usage = Usage()            # 整个 run 累计 token
last_usage = Usage()       # 最后一次调用的 usage（收尾紧凑时作为输入量参考）
tool_results = []          # 所有工具结果
changed_files = []         # write/edit 成功改过的路径
final_text = ""            # 最后一段模型文本（RunResult.text）
stop_reason = "stop"       # 结束原因：stop / max_turns / aborted / plan_rejected / api_error / error
turns = 0
```

`clamp_effort`：把配置里的 effort 钳制到 provider 支持的档位内（比如配了 `high` 但模型只支持 `low/medium`）。

`agent_start` 事件：记录工作区/环境指纹、provider、模型、effort。**replay 时用它判断"环境是否漂移"**——指纹对不上说明重跑条件不一致。

---

## 5. 主循环入口（130–162 行）

```python
step = 0
user_turn = True
display_turn = session.user_turns() if session is not None else 1
while True:
```

每个 `while` 迭代 = **一次模型调用 + 可能的工具批次**。

**三道门（循环顶部）**：

1. **abort 检查**（136–140 行）：`/stop` 是"轮间生效"——不打断正在进行的模型调用，在上一轮结束后的下一个循环入口才停机。置 `stop_reason="aborted"`，发 `error` 事件，break。
2. **max_turns 检查**（141–144 行）：`config.max_turns <= 0` 表示不限；超限就 `stop_reason="max_turns"` 收场。
3. **turn 编号**（145–146 行）：`turn = turn_offset + step`，跨任务连续编号；`turns` 是本 run 的步数。

**输入审计**（148–158 行）：

```python
request_items = items + extra_input
input_audit = item_audit(request_items, ws.root)
input_audit["workspace_fingerprint"] = workspace_fingerprint(workspace_manifest(ws.root))
bus.emit("turn_start", ...)
bus.emit("message_start", turn=turn)
```

每轮请求都拍一份指纹——replay 时对比"输入是否一致"。`turn_start` 之后 `user_turn` 翻成 `False`（这一轮是模型说话）。

---

## 6. 模型调用（160–162 行）

```python
response = _complete_with_overflow(
    model, items, tools, instructions,
    on_delta=on_delta, workspace=ws.root,
    context_window=config.provider.context_window,
    compact=compact, session=session, extra=extra_input,
)
```

注意传的是 `items`（干净历史），`extra_input` 走单独的 `extra` 参数——**临时上下文只在这一次请求里存在**，不会被记进历史。细节见第 12 节。

调用完再查一次 abort（163–167 行）：模型调用期间用户可能按了 `/stop`，此时把已产出的文本保留进 `final_text` 再退出。

**记账与录制**（168–177 行）：

```python
usage.add(response.usage)          # 累计 token
last_usage = response.usage        # 最后一轮的 usage
bus.record_response(turn, response.output, {...}, input_audit=input_audit)
```

`record_response` 把模型**原始响应**连同输入指纹落盘——这是 replay 的基础：重跑时可以不联网，直接按录像回放模型输出。

**把模型输出追加进上下文**（178–179 行）：响应里的每个 item（文本/思考/工具调用）都 `_push` 进 `items`。下一轮模型调用时，它看到的上下文就包含了自己刚才的调用记录。

---

## 7. 消息收尾与 `message_end`（180–197 行）

```python
text = extract_text(response.output)
thinking = extract_thinking(response.output)
if text: final_text = text
calls = parse_function_calls(response.output)
hide_text = any(call.name == "plan" for call in calls)
```

- `final_text` 随每次有文本的输出滚动更新，最后一段自然成为 run 的结论。
- `hide_text`：模型调了 `plan` 工具时**不回显正文**——计划确认弹窗已经展示过内容，终端再打一遍是噪音。

`message_end` 事件带上 `text` / `thinking` / `response_id` / `streamed`，UI 层据此结束一个气泡。

---

## 8. 分支一：无工具调用（198–215 行）

**无工具调用 = 模型认为任务做完了。** 但退出前先看一眼排队输入：

```python
pending = []
if queue:
    pending.extend(queue.drain_steer())   # steer：用户运行中补的话
    pending.extend(queue.drain_follow())  # follow：本轮结束后再说的
if pending:
    for msg in pending:
        _push(items, session, {"role": "user", "content": msg})
    user_turn = True
    display_turn += 1
    continue      # 不退出，用新消息再跑一轮
stop_reason = "stop"
break
```

两条通道的语义区别：

- **steer**：中途纠偏，下一轮就注入。
- **follow**：排队追加，本轮（模型已说完）结束后投递——在这里消化，变成新的 user 消息再跑一轮。

没排队输入才真正 `stop`。

---

## 9. 分支二：执行工具（216–250 行）

```python
batch = _run_tools(runtime, calls, bus, turn)
tool_results.extend(batch)
```

`_run_tools`（细节见第 14 节）：拍 before 快照 → 逐个发 `tool_execution_start`（参数脱敏）→ 批量执行 → 拍 after 快照 → 逐个发 `tool_execution_end`（含安全裁决、工作区 diff）。

**结果回灌上下文**（222–235 行）：每个工具结果追加一条 `function_call_output` 进 `items`：

```python
{
    "type": "function_call_output",
    "call_id": result.call_id,   # 和模型那次调用配对
    "output": result.output,
    "is_error": bool(result.is_error),
    "blocked": bool(result.blocked),
}
```

模型下一步就能看到每个调用的结果。`write` / `edit` 成功的路径记进 `changed_files`（去重），最后进 meta 供 UI/审计展示。

---

## 10. Harness 变更 → 重拼系统提示（236–243 行）

```python
if store.dirty:
    store.dirty = False
    instructions = system_prompt(ws.root, trusted=trusted, harness=store.merged())
    if session is not None:
        session.cache_epoch += 1
        _sync_cache_key(model, session)
```

`harness` 工具（agent 自己改的 prompt/memory 笔记）会标 `store.dirty`。笔记一变：

1. 系统提示要**重拼**（笔记内容在提示里）；
2. `cache_epoch` 自增 + 同步 cache key——**前缀变了，旧的 prompt cache 不能再命中**，必须开新纪元，否则会缓存到过期提示。

---

## 11. 会话持久化与循环尾部的三个检查（244–271 行）

```python
if session is not None:
    session.approvals = [list(k) for k in safety.memory]   # 本轮新增的批准落盘
    extra_input = ephemeral_items(ws.root, plan)           # 临时上下文刷新（计划可能刚变）
    session.persist()
bus.emit("turn_end", turn=turn, tool_calls=len(calls))
```

之后按顺序三个检查，任何一个命中就 break 或 continue：

1. **plan 被用户拒绝**（256–260 行）：`plan` 工具返回 `is_error` 且输出含 "rejected" → `stop_reason="plan_rejected"`，发 `plan_rejected` 事件，等用户给新指示。
2. **abort**（261–265 行）：和入口检查同逻辑，工具跑完后用户已按 `/stop`。
3. **steer 注入**（266–271 行）：

```python
steers = queue.drain_steer() if queue else []
if steers:
    bus.emit("steer_delivered", turn=turn, count=len(steers))
    user_turn = True
    display_turn += 1
for msg in steers:
    _push(items, session, {"role": "user", "content": msg})
```

steer 消息作为新 user 消息并入上下文，**下一轮模型调用直接看到**，然后循环自然回到顶部。这就是"运行中改需求不用等"的实现。

---

## 12. 异常处理（272–290 行）

循环整体包在 `try` 里，三种收尾：

```python
except KeyboardInterrupt:        # 运行中 Ctrl+C
    runtime.abort_running()      # 中止后台 bash 作业
    stop_reason = "aborted"
    final_text = final_text or "interrupted"
    bus.emit("error", message="interrupted")
    if queue:
        queue.abort.set()
    else:
        raise                    # 没有 queue（非交互嵌入）就原样上抛
except APIError as exc:          # 模型 API 报错（含 transient/status 信息）
    stop_reason = "api_error"
    final_text = str(exc)
    bus.emit("error", message=str(exc), transient=exc.transient, status=exc.status)
except Exception as exc:         # 兜底
    stop_reason = "error"
    final_text = f"agent error: {exc}"
    bus.emit("error", message=str(exc))
```

设计点：**异常不向上炸**，全部转成 `stop_reason` + `error` 事件 + 保留已完成内容。唯一例外是没有 queue 时把 `KeyboardInterrupt` 原样抛出（嵌入方自己处理信号）。

---

## 13. 收尾紧凑（292–318 行）

```python
if compact and stop_reason in {"stop", "max_turns"}:
```

只在**正常结束**（自然完成或跑满轮数）时顺手做一次紧凑：把旧历史压成摘要，为下一个任务省输入 token。

```python
compacted, extra, stats = compact_history(
    items, model, ws.root,
    input_tokens=last_usage.input_tokens,
    context_window=config.provider.context_window,
    plan_text=plan.render() if plan.steps else "",
)
if compacted is not items:
    items[:] = compacted         # 原地替换列表内容
if session is not None:
    session.apply_compact(items) # 紧凑是叠加层，不销毁会话树
    if stats.did:
        session.compactions += 1
        session.last_compact = stats.as_dict()
if stats.did:
    bus.emit("compact", **stats.as_dict(), epoch=...)
```

外层是宽 `except`（317–318 行）：**紧凑失败不能丢掉已完成的 run**，只发一条 `compact skipped` 错误事件。

---

## 14. 终局审计与返回（320–375 行）

**工作区 diff**（320–322 行）：

```python
final_manifest = workspace_manifest(ws.root)
final_workspace_fingerprint = workspace_fingerprint(final_manifest)
changes = workspace_changes(initial_manifest, final_manifest)
```

和开机的初始快照对比，得出这次 run 到底改了哪些文件。

`agent_end` 事件（323–335 行）：turns、stop_reason、final_text、三类 token 计数、task_id、终局指纹、changes。

`meta` 字典（336–358 行）：上面事件内容 + 初始指纹、environment 指纹、provider/base_url/api 全记录、effort 三件套（原始值/钳制后值/支持档位）、工具调用总数和错误数。`extra_meta`（调用方附加字段）合并进去，`bus.write_meta(**meta)` 落盘。

最后把批准记忆回填 session（360–361 行），返回 `RunResult`（363–375 行）：run_id、text、turns、usage、tool_results、stop_reason、events_path、items（完整上下文，供下次 run 续接）、last_usage、changed_files、task_id。

---

## 15. `_push`（378–386 行）

```python
def _push(items, session, item):
    items.append(item)
    if session is not None:
        session.append_item(item, to_view=False)
        session.persist()
```

往上下文追加一条消息。`to_view=False` 的关键：`run_agent` 里 `items` 就是 `session.items` 本身（第 101 行的别名），`append` 已经更新了视图，所以会话树只需补记录、不用重建视图。每次 push 都 `persist()`——进程随时崩了，会话树都完整。

---

## 16. `_sync_cache_key`（389–393 行）

一行逻辑：session 的 `cache_key` 变了（epoch 自增时生成新 key），同步到 model 客户端的 `cache_key` 属性。模型客户端下次请求就用新 key 发 `prompt_cache_key`——旧缓存自然失效，新前缀从第一轮就重建缓存。

---

## 17. `_complete_with_overflow`（395–434 行）

调一次模型，带**上下文溢出自愈**：

```python
_sync_cache_key(model, session)
try:
    return model.complete(items + extra, tools, instructions, on_delta=on_delta)
except KeyboardInterrupt:
    raise                                   # Ctrl+C 原样上抛，交给 run_agent
except Exception as exc:
    if not compact or not is_context_overflow(exc):
        raise                               # 不是溢出错误（或紧凑已关）：原样上抛
    compacted, _extra, stats = compact_history(
        items, model, workspace,
        input_tokens=context_window,        # 用满窗口当"当前用量"
        context_window=context_window, force=True,
    )
    items[:] = compacted                    # 原地压缩
    if session is not None:
        session.apply_compact(items)
        ...
        session.persist(rewrite=True)       # 重写 jsonl 落盘
    _sync_cache_key(model, session)
    return model.complete(items + extra, tools, instructions, on_delta=on_delta)
```

要点：

- 各 provider 的"上下文超长"报错措辞不同，`is_context_overflow` 统一识别。
- 溢出时 `force=True` 强制紧凑（不管当前用量估算），压完**重试一次**，第二次还溢出就正常抛错。
- `items[:] = compacted` 原地替换，调用方持有的列表引用不变。

---

## 18. `_run_tools`（437–472 行）

一次工具批次的"审计三明治"：

```python
before = workspace_manifest(root); before_fp = fingerprint(before)   # ① 拍前快照
for call in calls:
    bus.emit("tool_execution_start", ..., args=redact_tool_args(...), ...)   # ② 脱敏后发开始事件
results = runtime.execute_batch(calls)                                     # ③ 批量执行
after = workspace_manifest(root); after_fp = fingerprint(after)            # ④ 拍后快照
changes = workspace_changes(before, after)
for call, result in zip(calls, results):
    audit = tool_audit(call, result)
    bus.emit("tool_execution_end", ..., result=redact_tool_output(...),
             safety_decision=audit["decision"], ...)                       # ⑤ 发结束事件
```

- 参数/输出都**脱敏**后才进事件流——密钥类字段不出现在日志里。
- 每个结束事件带 `safety_decision` / `safety_reason` / `safety_source`（SafetyGate 的裁决：allow/ask/deny 及依据）和 `workspace_changes`（这次工具批次改了哪些文件）。
- 返回值就是结果列表，回灌上下文的活由 `run_agent` 做。

---

## 19. EventBus：一次 emit，三处收益

`core/events.py` 里的 `EventBus`。类 docstring 一句话点题：

> 一次 emit 同时写盘 + 喂订阅者，UI 只是事件流的一个视图。

### 机制

```python
def emit(self, type_, **data):
    event = {"type": type_, "run_id": self.run_id, "ts": _now(), **data}
    with self.events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")   # ① 先落盘
    for listener in list(self.listeners):
        listener(event)                                          # ② 再喂订阅者（副本遍历，订阅者内退订也安全）
    return event
```

一次运行 = 一个 `run_dir`（`runs_dir/<run_id>`），三个文件：

| 文件 | 谁来写 | 内容 |
|---|---|---|
| `events.jsonl` | `emit()` 逐行追加 | 全部事件，一条一行 |
| `responses.jsonl` | `record_response()` | 每轮的**模型原始响应** + 输入审计指纹 + 输出哈希 |
| `meta.json` | `write_meta()` | 收尾 meta（provider/模型/token/指纹/改动） |

`run_id` 由 `new_run_id()` 生成（时间戳 + 8 位随机后缀），最后进 `RunResult.run_id`，`/replay` 靠它找回运行（`load_run` 支持前缀匹配，也支持按 `session_id` 查）。

### 在 loop.py 里的三个作用

**1. UI 只是订阅者，循环不认识 UI。**
`run_agent` 只管 `bus.emit(...)`，`on_event` 回调不过是 `bus.subscribe(on_event)` 挂上去的一个监听者。好处：刷终端、写日志、发指标可以同时挂多个订阅者，主循环一行都不用改。

**2. 审计 + replay 的数据源。**
这是最值钱的部分：

- `bus.record_response(turn, output, usage, input_audit=...)` 把模型**原始响应**录下来。`ui/replay.py` 读 `responses.jsonl` 喂给 `ScriptedModel`，就能**不发 API、不花钱**重跑一次运行。
- 回放时对比 `input_audit`（每轮请求的上下文指纹）和 `output_sha256`（响应哈希），判断两次运行的输入/输出是否一致。
- `tool_execution_start` / `tool_execution_end` 里的 `args` / `safety_decision` / `workspace_fingerprint` / `workspace_changes` 让 replay 能对比"两次运行调了同样的工具没、裁决是否相同"。
- `agent_start` 的 `environment_fingerprint` 用来判定重跑时环境是否漂移。

**3. 崩溃安全。**
每个事件即时追加写盘，进程中途崩掉，已完成部分的事件流依然完整可读。读侧 `_load_jsonl` 会跳过空白行和 JSON 解析失败的行——崩溃留下的半行不会炸掉整个读取。

### loop.py 发出的全部事件类型

| 事件 | 行号 | 关键字段 |
|---|---|---|
| `agent_start` | 113 | 工作区/环境指纹、provider、model、effort、reasoning |
| `turn_start` | 149 | turn、step、user、display_turn、**input_audit**、环境指纹 |
| `message_start` | 157 | turn |
| `message_end` | 197 | text、thinking、response_id、streamed、hide_text |
| `turn_end` | 212 / 253 | tool_calls、queued_inputs |
| `tool_execution_start` | 444 | tool_name、**脱敏 args**、args_sha256、工作区指纹 |
| `tool_execution_end` | 459 | is_error、blocked、**脱敏 result**、safety_decision/reason/source、workspace_changes |
| `steer_delivered` | 268 | turn、count |
| `plan_rejected` | 258 | turn |
| `compact` | 313 | 紧凑统计 + epoch |
| `error` | 135 / 175 / 263 / 278 / 286 / 290 / 318 | message、transient、status |
| `agent_end` | 324 | turns、stop_reason、text、三类 token、指纹、changes |

`error` 出现七次：运行中被 `/stop`（135/175/263）、`KeyboardInterrupt`（278）、API 报错（286）、兜底异常（290）、紧凑被跳过（318）——**所有异常路径都落到同一条事件流里**，读日志时用一条 `grep "error"` 就能看全。

---

## 一图流：一次 run 的生命周期

```
组装组件（ws/bus/safety/plan/runtime/指纹/系统提示）
        │
        ▼
   agent_start ──► while True:
                     ├─ abort? → aborted
                     ├─ max_turns? → max_turns
                     ├─ turn_start + 输入审计
                     ├─ 调模型（溢出→紧凑→重试一次）
                     ├─ record_response + 输出入上下文
                     ├─ message_end
                     ├─ 无工具调用?
                     │    ├─ 有排队 steer/follow → 并入上下文，continue
                     │    └─ 没有 → stop
                     ├─ 有工具调用 → _run_tools（快照+审计+执行）
                     ├─ 结果入上下文；harness 脏 → 重拼提示 + 新缓存纪元
                     ├─ session.persist
                     ├─ plan 被拒? → plan_rejected
                     ├─ abort? → aborted
                     └─ steer → 并入上下文，continue
        │
        ▼
   异常？→ api_error / error（不抛出，保留已完成内容）
        │
        ▼
   正常结束 → 顺手紧凑（失败也不影响 run）
        │
        ▼
   工作区 diff → agent_end → write_meta → RunResult
```
