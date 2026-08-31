# `tools/tools.py` 逐段讲解

> 本篇讲工具层。上游是 [core/loop.py](../../../core/loop.py)（主循环调用 `ToolRuntime.execute_batch`），下游是 [tools/safety.py](safety.md)（安全裁决）、[tools/workspace.py](workspace.md)（文件读写）、[core/checkpoint.py](../core/checkpoint.md)（undo 快照）、[core/truncate.py](../../../core/truncate.py)（输出截断）。

一句话职责：**声明 13 个工具（名字/description/JSON schema/执行器），并在执行前后统一做参数校验、安全裁决、快照、截断和并行调度。**

- 行数：999 行（项目第二长的文件）
- 依赖：
  - [tools/workspace.py](workspace.md) —— 所有文件访问收口，路径解析 + 越界拦截
  - [tools/safety.py](safety.md) —— `SafetyGate.review`（allow/ask/deny）、`is_sensitive_path`
  - [core/checkpoint.py](../core/checkpoint.md) —— write/edit/bash 前存回滚快照
  - [core/truncate.py](../../../core/truncate.py) —— 工具输出的行/字节截断 + 溢写文件
  - [core/plan.py](../core/plan.md) —— `PlanStore` / `PlanRejected`，plan 工具的状态机
  - [harness/harness.py](../harness/harness.md) —— `HarnessStore.dispatch`，harness 工具的后端
  - [tools/rgfiles.py](rgfiles.md) —— `grep_files` / `glob_files`（rg 优先 + 纯 Python 回退）
  - [tools/web.py](web.md) —— `search_web` / `fetch_url`
  - [core/types.py](../core/types.md) —— `FunctionCall` / `ToolResult`
- 被谁用：
  - [core/loop.py](../../../docs/loop-explained.md) —— 主循环唯一入口：`tool_schemas()` 拿 schema、`parse_function_calls()` 拆调用、`execute_batch()` 跑一批
  - [ui/app/commands.py](../ui/app-commands.md) —— `/jobs` 用 `format_jobs()` / `drain_job_events()` / `kill_job()`
  - [ui/graph.py](../ui/graph.md) —— 用 `parse_function_calls()` 把 item 变成 DAG 里的工具节点

## 目录

- [1. 导入、类型别名与常量](#1-导入类型别名与常量1-37-行)
- [2. `_Job` 与全局作业表](#2-_job-与全局作业表40-53-行)
- [3. `ToolSpec`：一个工具的完整声明](#3-toolspec一个工具的完整声明56-67-行)
- [4. 工具清单速查表](#4-工具清单速查表)
- [5. `default_tools()`：12 个内置工具声明](#5-default_tools12-个内置工具声明70-235-行)
- [6. `tool_schemas()`：转成模型的 function schema](#6-tool_schemas转成模型的-function-schema238-249-行)
- [7. `ToolRuntime.__init__`：注册表组装](#7-toolruntime__init__注册表组装252-382-行)
- [8. `ToolRuntime` 方法速查表](#8-toolruntime-方法速查表)
- [9. `execute_batch()`：准备 + 分组调度](#9-execute_batch准备--分组调度413-441-行)
- [10. `_prepare()`：校验 + 安全门](#10-_prepare校验--安全门443-466-行)
- [11. `_run()`：快照 → 执行 → 截断](#11-_run快照--执行--截断468-493-行)
- [12. 计划拒绝拦截、快照与输出后处理](#12-计划拒绝拦截快照与输出后处理495-533-行)
- [13. `prepare_arguments()`：对模型的宽容校正](#13-prepare_arguments对模型的宽容校正536-561-行)
- [14. `validate_arguments()`：按 schema 严校验](#14-validate_arguments按-schema-严校验563-591-行)
- [15. `_execution_groups()`：读并行、写串行](#15-_execution_groups读并行写串行594-622-行)
- [16. `parse_function_calls()`：解析模型输出](#16-parse_function_calls解析模型输出625-649-行)
- [17. 只读工具：read / ls / grep / glob](#17-只读工具read--ls--grep--glob652-707-行)
- [18. 写工具：`_guard_write` / write / edit](#18-写工具_guard_write--write--edit709-757-行)
- [19. 联网工具与 `py_compile` 快检](#19-联网工具与-py_compile-快检759-792-行)
- [20. `_bash()`：前台执行与超时杀](#20-_bash前台执行与超时杀794-851-行)
- [21. `_start_job()`：后台作业的一生](#21-_start_job后台作业的一生853-899-行)
- [22. `bash_poll` / `bash_kill` / `_get_job`](#22-bash_poll--bash_kill--_get_job901-938-行)
- [23. 作业表的外围接口](#23-作业表的外围接口940-997-行)

## 1. 导入、类型别名与常量（1–37 行）

导入按"工具层需要什么"组织：checkpoint（回滚）、harness（持续学习）、plan（计划）、rgfiles（搜索实现）、safety（裁决）、truncate（截断）、types（数据结构）、web（联网）、workspace（文件）。

四个类型别名定义了整个工具层的"插件接口"：

```python
Executor = Callable[[dict[str, Any], Workspace, OnUpdate | None], str]   # 执行器签名
ExecutionMode = Literal["parallel", "sequential"]                        # 并行（只读）/ 串行（写）
```

`OnUpdate`（31 行）是工具执行中的**增量输出回调**：`bash` 每读到一行就 `on_update(line)`，UI 据此实时打印。它一路从 [loop.py](../../../docs/loop-explained.md) 的 `on_tool_update` 传进来——工具层不认识终端。

`FOREGROUND_TIMEOUT = 120`（37 行）：前台 bash 默认超时。**选 120 秒而不是更短**，是因为 `pip install`、测试套件这类命令常常超过 30 秒；而更长的超时会让用户等得没有反馈。真正的长任务走 `background=true` 通道，不受这个值约束。

## 2. `_Job` 与全局作业表（40–53 行）

```python
@dataclass
class _Job:
    job_id: str
    proc: subprocess.Popen[str]
    log_path: Path
    command: str
    notified: bool = False

JOBS: dict[str, _Job] = {}      # 全局后台作业表
JOBS_LOCK = threading.Lock()
```

两个设计点：

- **作业表是模块级全局，不是 `ToolRuntime` 的实例字段**。因为一个 REPL 进程里可能有多个 `ToolRuntime`（每个 run 一个），但后台作业必须跨 run 存活——这一轮起的 dev server，下一轮还要能 `bash_poll`。代价是作业表是进程内单例，不随 run 结束清理，只能靠 `atexit` 收尾。
- `notified` 标记"这个作业的退出事件已经播报过"，配合 `drain_job_events()` 实现**每作业只提醒一次**，避免 UI 反复刷同一条退出通知。

## 3. `ToolSpec`：一个工具的完整声明（56–67 行）

```python
@dataclass
class ToolSpec:
    name, description, parameters   # 给模型看
    readonly: bool                  # 是否只读（安全门与调度都用）
    execute: Executor               # 给运行时跑
    execution_mode: ExecutionMode   # 并行 / 串行
    truncate: Literal["head","tail","none"]   # 输出截断策略
```

一个 dataclass 同时装下**给模型看的 schema** 和**给运行时用的元信息**——所以新增一个工具只要加一个 `ToolSpec`，不用改 `tool_schemas`、`execute_batch`、安全门三处代码。

三个字段值得单独说：

- `readonly`：既影响安全门（`safety.py` 的 `READ_ONLY` 集合是另一份硬编码列表），也影响调度（只读才能并行）。
- `truncate="head"` / `"tail"` / `"none"`：**保头**给 `read` / `web_*`（开头是路径或标题，信息密度高）；**保尾**给 `bash` / `bash_poll`（命令的结果总在最后，测试失败信息在末尾）。`ls` / `glob` / `edit` 不截断——它们的输出本来就有条数上限。

## 4. 工具清单速查表

| 工具 | 声明行号 | 执行器 | 职责 | 模式 | 截断 | 风险 |
|---|---|---|---|---|---|---|
| `read` | 74 | `_read` (652) | 读文件（offset/limit） | parallel | head | 只读 |
| `ls` | 92 | `_ls` (663) | 列一个目录 | parallel | none | 只读 |
| `grep` | 105 | `_grep` (670) | 正则搜文件内容 | parallel | none | 只读 |
| `glob` | 125 | `_glob` (693) | 按模式找文件路径 | parallel | none | 只读 |
| `write` | 145 | `_write` (718) | 创建/覆盖文件 | sequential | none | **写** |
| `edit` | 161 | `_edit` (727) | 唯一匹配替换 | sequential | none | **写** |
| `bash` | 182 | `_bash` (794) | 执行命令（前台/后台） | sequential | tail | **写 + 任意副作用** |
| `web_search` | 205 | `_web_search` (759) | 联网搜索 | parallel | head | 只读（外网） |
| `web_fetch` | 222 | `_web_fetch` (768) | 抓 URL 文本 | parallel | head | 只读（外网） |
| `plan` | 273 | `ToolRuntime._plan` (402) | 提交计划步骤等批准 | sequential | none | 改计划状态 |
| `harness` | 308 | `ToolRuntime._harness` (409) | 持久化 prompt/memory 笔记 | sequential | none | 改持久笔记 |
| `bash_poll` | 351 | `_bash_poll` (901) | 读后台作业输出 | parallel | tail | 只读 |
| `bash_kill` | 368 | `_bash_kill` (918) | 杀后台作业 | sequential | none | 自己的作业 |

`plan` / `harness` / `bash_poll` / `bash_kill` 不在 `default_tools()` 里，而是在 `ToolRuntime.__init__` 里动态注入（见第 7 节）——因为它们需要闭包捕获 `self`。

## 5. `default_tools()`：12 个内置工具声明（70–235 行）

每个 `ToolSpec` 的 `parameters` 是一份手写的 JSON Schema，全部带 `additionalProperties: False`。**为什么不用 pydantic 从类型生成？** 因为 description 是提示词工程的一部分，需要逐字段写清楚（比如 `read` 的 `offset` 要写明"1-based start line"），自动生成做不到这个精度。

几处值得注意的提示词设计：

- `grep` 的 description 里写"Not for listing filenames — use glob"，`glob` 里写"Returns paths only; does not read contents. ls lists one directory"——**三个搜索工具的边界写在彼此的 description 里**，模型最容易犯的错是在它们之间选错。
- `bash` 的 description 明确写"For install/tests/servers, set background=true: returns job_id immediately. Then STOP and tell the user the job_id. Do not poll in this turn."——**显式禁止模型在同一轮里轮询**。否则模型会连续调十几次 `bash_poll` 把上下文填满，而后台任务还没跑完。
- `edit` 的 description 写"If old_string is not unique, set replace_all=true or add more context"——把报错的解决办法提前告诉模型，省一轮往返。

`write` 是唯一没有 `readonly` 保护的"整体覆盖"操作；`edit` 用唯一匹配约束，比 `write` 安全，所以工具描述里没有优先推荐 `write`。

## 6. `tool_schemas()`：转成模型的 function schema（238–249 行）

```python
def tool_schemas(tools=None) -> list[dict]:
    return [{"type": "function", "name": ..., "description": ..., "parameters": ...} for spec in specs]
```

纯字段映射，把 `ToolSpec` 的四个"给模型看"的字段挑出来组成 OpenAI 的 function schema。**没有 `strict: true`**——Responses API 的 strict 模式要求所有字段都 `required` 且不允许可选参数，这里刻意保留可选参数（`read` 的 offset/limit、`bash` 的 timeout）。

返回值由 [loop.py](../../../docs/loop-explained.md) 第 93 行 `tools = tool_schemas(list(runtime.tools.values()))` 拿走，进每一轮模型请求。

## 7. `ToolRuntime.__init__`：注册表组装（252–382 行）

构造分六步：

**① 依赖兜底（264–270 行）**：`plan` 和 `harness` 不传就自己 new。注意 `PlanStore(ask=safety.ask, interactive=safety.interactive)`——**计划确认弹窗复用安全门的 y/N 回调**，两条确认路径走同一个交互通道。

**② plan 工具注入（272–305 行）**：`if not any(spec.name == "plan" ...)` 判断是否已存在，再 append。执行器是 `lambda a, w, u=None: self._plan(a)`——闭包捕获 `self`，这样工具能访问 runtime 的 `PlanStore`。

plan 的 schema 里 `steps` 是一个对象数组（`content` + `status` 枚举）。description 是一段完整的状态机说明：提交 → 等批准 → 逐步标记 `in_progress` / `done`，且"若计划被拒，先提交修订计划再 write/edit"。

**③ harness 工具注入（307–335 行）**：同样模式。参数是 `action`（list/create/update/delete）+ `kind`（prompt/memory）+ `id`/`title`/`content`/`path`/`global` 的扁平结构，而不是嵌套——**扁平参数对模型更友好**，嵌套对象容易填错层。

**④ 注册表与状态（337–342 行）**：

```python
self.tools = {spec.name: spec for spec in specs}
self.checkpoints = CheckpointStore.for_workspace(workspace.root)
self._proc: subprocess.Popen[str] | None = None
self._decisions: dict[str, Any] = {}
```

`self.tools` 是**字典而非列表**——后面查表按名字 O(1)。`_decisions` 是"call_id → 安全裁决"的中间暂存：`_prepare` 写入、`_run` 读出，把"裁决"和"执行"两个阶段的裁决结果传过去。

**⑤ bash 换壳（344–348 行）**：

```python
if "bash" in self.tools and self.tools["bash"].execute is _bash:
    self.tools["bash"] = replace(spec, execute=lambda a, w, u=None: _bash(a, w, u, on_proc=self._set_proc))
```

用 `dataclasses.replace` 换掉执行器，目的是**把当前前台进程句柄记到 `self._proc`**，供 `abort_running()` 在 Ctrl+C / `/stop` 时杀掉。判断条件带 `is _bash` 是为了**幂等**——换过一次后 `execute` 变成 lambda，就不再重复包装。

**⑥ `bash_poll` / `bash_kill` 注入（350–382 行）**：用 `setdefault` 而非直接赋值，允许调用方预先传入自定义版本覆盖。这两个工具描述的就是"怎么管后台作业"，是 `bash` 的配套。

## 8. `ToolRuntime` 方法速查表

| 方法 | 行号 | 职责 |
|---|---|---|
| `__init__` | 255 | 组装注册表、注入 plan/harness/bash_* 工具 |
| `_set_proc` | 384 | 记录当前前台进程句柄 |
| `task_id` (property) | 388 | 当前 checkpoint 任务 ID |
| `begin_task` | 391 | 开一个 checkpoint 任务（`loop.py` 每 run 调一次） |
| `abort_running` | 396 | 杀当前前台 bash（Ctrl+C / `/stop`） |
| `_plan` / `_harness` | 402 / 409 | 两个注入工具的执行器（转发给 PlanStore / HarnessStore） |
| `execute` | 413 | 单条执行（就是 `execute_batch([call])[0]`） |
| `execute_batch` | 416 | **主入口**：准备 + 分组调度 |
| `_prepare` | 443 | 参数校验 + 安全裁决；被拒直接产出错误结果 |
| `_run` | 468 | 单个已放行工具：快照 → 执行 → 截断 |
| `_rejected_plan_block` | 495 | 计划被拒后拦截 write/edit |
| `_checkpoint` | 506 | 改文件/跑 bash 前存快照 |
| `_after` | 521 | 输出截断后处理 |

## 9. `execute_batch()`：准备 + 分组调度（413–441 行）

```python
def execute_batch(self, calls: list[FunctionCall]) -> list[ToolResult]:
    prepared = [self._prepare(call) for call in calls]     # ① 逐个准备
    results = [early for *_, early in prepared]            # ② 预置"提前失败"的结果
    runnable = [...]                                       # ③ 筛出真正要跑的
    for group in _execution_groups(runnable):              # ④ 分组
        sequential = group[0][2].execution_mode == "sequential"
        if sequential or len(group) <= 1:
            for idx, call, spec, args in group:            # 串行跑
                results[idx] = self._run(spec, call, args)
        else:
            with ThreadPoolExecutor(max_workers=min(8, len(group) or 1)) as pool:
                futs = {pool.submit(self._run, spec, call, args): idx for ...}
                wait(futs)
                for fut, idx in futs.items():
                    results[idx] = fut.result()
    return [item if item is not None else ToolResult(..., "internal error", True) ...]
```

四个设计点：

1. **准备与执行分离。** 准备阶段（校验 + 安全门）全部**串行**完成，之后才调度。安全门里的 y/N 弹窗必须串行——不能同时弹三个确认框。
2. **结果按索引回填。** `results` 初始化时就按 `prepared` 的顺序占好位，被拒/无效参数的调用填 `early` 错误结果，执行的填 `_run` 结果。**返回顺序严格等于输入顺序**，这是 OpenAI 协议的要求（`function_call_output` 靠 `call_id` 配对，但顺序一致让 [loop.py](../../../docs/loop-explained.md) 的 `zip(calls, batch)` 直接成立）。
3. **并行度上限 8**（`min(8, len(group) or 1)`）。只跑只读工具，主要是 `read` / `grep` / `web_fetch` 这类有 IO 等待的；8 是"多路并发够用、又不至于打爆磁盘/限流"的折中。
4. **兜底 `internal error`**：任何位置还是 `None`（逻辑漏洞）都填成 `is_error=True` 的结果，而不是让异常冒泡。

`zip(results, prepared)` 的兜底只覆盖两个列表等长的情况——它们本来就等长，所以这只是防御性写法。

## 10. `_prepare()`：校验 + 安全门（443–466 行）

三道关卡，每道都可能直接产出"提前失败"的 `ToolResult`：

```python
spec = self.tools.get(call.name)
if spec is None:
    return None, None, ToolResult(..., f"unknown tool: {call.name}", is_error=True)
try:
    args = prepare_arguments(spec, call.arguments)
    validate_arguments(spec, args)
except Exception as exc:
    return spec, None, ToolResult(..., f"invalid arguments: {exc}", is_error=True)
verdict = self.safety.review(FunctionCall(call.call_id, call.name, args, call.raw_arguments))
self._decisions[call.call_id] = verdict
if verdict.decision == "deny":
    return spec, args, ToolResult(..., f"blocked by safety ({verdict.source}): {verdict.reason}",
                                  is_error=True, blocked=True, safety_*=...)
```

- **未知工具**：模型幻觉出的工具名。返回错误文本而不是抛异常——**模型下次读到这条错误就知道换个工具**，run 还能继续。
- **参数校验失败**：错误信息是 `f"invalid arguments: {exc}`，而 `exc` 来自 `validate_arguments` 的具体消息（如 `missing path`、`old_string should be string`）。**错误信息本身是给模型的提示词**。
- **安全门 `deny`**：`blocked=True` 单独标记，因为"被安全门拦下"和"工具自己报错"在 UI 里显示不同（前者要标红提示用户去确认）。裁决三元组 `safety_decision` / `safety_reason` / `safety_source` 原样带进 `ToolResult`，最后进 [tools/audit.py](audit.md) 的事件流。

注意传给 `safety.review()` 的是**修正后的 `args`**（`prepare_arguments` 的输出），但保留 `call.raw_arguments` 原始 JSON 串供审计——安全门要基于真实参数裁决，审计要能复现模型到底发了什么。

## 11. `_run()`：快照 → 执行 → 截断（468–493 行）

```python
verdict = self._decisions.get(call.call_id)
fields = {safety_decision/reason/source...}
blocked = self._rejected_plan_block(spec)
if blocked: return ToolResult(..., blocked, is_error=True, **fields)
try:
    self._checkpoint(spec, args)                       # ① 改前快照
    output = spec.execute(args, self.workspace, self.on_update)   # ② 执行
    output = self._after(spec, output)                 # ③ 截断
except KeyboardInterrupt:
    raise                                              # Ctrl+C 必须穿透
except Exception as exc:
    return ToolResult(..., f"{type(exc).__name__}: {exc}", is_error=True, **fields)
return ToolResult(call.call_id, call.name, output, **fields)
```

**为什么 `KeyboardInterrupt` 单独 `raise`？** 它要一路穿到 [loop.py](../../../docs/loop-explained.md) 的 `except KeyboardInterrupt`（那里调 `runtime.abort_running()` 杀后台作业、保留已完成内容）。如果在这里被宽 `except` 吞掉，Ctrl+C 就只中止了一个工具，run 会继续跑。

**为什么其他异常一律 `f"{type(exc).__name__}: {exc}"`？** 见第 24 节第 1 条——工具的错误必须变成"模型能读懂的观察"，而不是中断 run。

## 12. 计划拒绝拦截、快照与输出后处理（495–533 行）

**`_rejected_plan_block`（495–504 行）**：只拦 `write` / `edit`，条件是 `not self.plan.confirmed and self.plan.rejected`。返回的错误文本直接告诉模型"先调 plan 工具提交修订计划"。这是 [core/plan.py](../core/plan.md) 里 `PlanRejected` 的另一半：**拒绝抛异常发生在 plan 工具里，拦截发生在这里**，模型被拒后想跳过计划直接改文件走不通。

**`_checkpoint`（506–519 行）**：

```python
if spec.name in {"write", "edit"}:
    target = _guard_write(self.workspace, raw)
    self.checkpoints.snapshot(target, tool=spec.name, task_id=self._task_id)
elif spec.name == "bash":
    self.checkpoints.snapshot_bash(command, self.workspace.resolve, task_id=self._task_id)
```

- write/edit：先 `_guard_write` 解析并拒敏感路径，再存快照。快照在**执行之前**存，存的是旧内容。
- bash：`snapshot_bash` 扫命令里的 `rm` / `mv`，对其文件参数存快照（只能识别字面量路径）。
- **整个方法包在 `try/except: return`**——快照失败静默。回滚是"尽力而为"的辅助功能，不能因为 `.wheel/checkpoints` 写不进去就阻塞主流程。

快照归属 `task_id`，由 `begin_task()` 开（`loop.py` 每个 run 调一次），`/undo-task` 按任务整体回滚。

**`_after`（521–533 行）**：

```python
if spec.truncate == "head": return apply(output, root, tail=False)
if spec.truncate == "tail":
    prefix = ""
    if output.startswith("exit="):
        first, _, rest = output.partition("\n")
        prefix, output = first + "\n", rest
        return apply(prefix + output, root, tail=True, keep_prefix=prefix)
    return apply(output, root, tail=True)
```

`keep_prefix` 是这里唯一的特殊逻辑：**bash 输出的 `exit=0` 行必须活过截断**。因为截断保尾（丢开头），而 `exit=` 在第一行——丢了模型就不知道命令是成功还是失败。`truncate.apply` 的 `keep_prefix` 参数正是为此设计的：只截 payload，头部原样保留，行/字节预算全花在真实输出上。

## 13. `prepare_arguments()`：对模型的宽容校正（536–561 行）

```python
if "_parse_error" in args:
    raise ValueError(args["_parse_error"])      # ① parse 阶段留下了解析错误
for key, schema in props.items():
    if isinstance(value, str) and expected in {"array", "object"}:
        try: parsed = json.loads(value)         # ② "[\"a\"]" → ["a"]
        except: continue                        # 解析失败就原样留着，交给 validate 报
    if isinstance(value, str) and expected == "boolean":
        if low in {"true","false","1","0","yes","no"}: out[key] = low in {"true","1","yes"}
```

**为什么需要这一层？** 模型（尤其小模型）常把数组/对象/布尔值序列化成字符串发过来。严格校验会直接报 `steps should be array`，模型下次多半还是发错。**先宽容修一遍、再严格校一遍**，比直接拒绝省好几轮往返。

`json.loads` 失败时 `continue` 而不是报错——原样留给 `validate_arguments` 报，错误判定集中在一处。

## 14. `validate_arguments()`：按 schema 严校验（563–591 行）

按顺序查五项，任一项失败抛 `ValueError`：

1. `required` 缺失 → `missing {key}`
2. `additionalProperties: False` 时的未知字段 → `unexpected {key}`
3. 类型不符 → `{key} should be {type}`。注意整数用 `type(value) is int`，**故意排除 `bool`**（Python 里 `bool` 是 `int` 的子类，`isinstance(True, int)` 为真）——否则模型传 `true` 给 `limit` 会被当成 1。
4. `enum` 越界 → `must be one of [...]`

## 15. `_execution_groups()`：读并行、写串行（594–622 行）

把一批调用切成若干组：**连续的只读调用合并成一组并行，每个写调用单独一组**。

```
[read, read, grep, write, edit, read] → [[read,read,grep], [write], [edit], [read]]
```

算法是单趟扫描：`current` 攒连续的 parallel 调用，遇到 sequential 就先把 `current` 落组、再把该调用单独成组。

**为什么写工具必须串行？** 两个 `edit` 同时改一个文件会互相覆盖；`bash` 之间可能有顺序依赖（先 `mkdir` 后 `cd`）。而只读工具没有副作用，并行是安全的。

一个副作用：**写的相对顺序在组内被保留，跨组也是按输入顺序执行的**（`execute_batch` 按组序遍历），所以模型发出 `[write A, write B]` 一定是先 A 后 B。

## 16. `parse_function_calls()`：解析模型输出（625–649 行）

```python
for item in output:
    if item.get("type") != "function_call": continue      # ① 只认 Responses 的 function_call item
    raw = item.get("arguments") or "{}"
    if not isinstance(raw, str): raw = json.dumps(raw)    # ② 容错：非字符串就序列化
    try:
        args = json.loads(raw) if raw.strip() else {}
        if not isinstance(args, dict): raise ValueError("arguments must be an object")
    except Exception as exc:
        args = {"_parse_error": str(exc), "_raw": raw}    # ③ 解析失败不抛，塞标记
    calls.append(FunctionCall(
        call_id=str(item.get("call_id") or item.get("id") or f"call_{len(calls)}"),
        ...))
```

- **只处理 Responses 协议的 `function_call` item。** Chat Completions 的 `tool_calls` 由 [core/model.py](../../../core/model.py) 在第 406–410 行归一化成同样的 `function_call` item（调 `function_call_item()`），所以这里只需认一种格式——**协议差异收敛在 model 层，工具层只认一种**。
- `call_id` 三级回退：`call_id` → `id` → 合成 `call_{n}`。`call_id` 是配对的钥匙（响应里的 `function_call_output` 用它对应），绝不能是 `None`。
- **解析失败不抛异常**，而是塞 `_parse_error` 伪参数，由 `prepare_arguments` 转成 `ValueError` 再变成错误 `ToolResult`。这样模型收到的是"你的 arguments 不是合法 JSON"而不是整个 run 崩掉。
- `raw_arguments` 保留原始字符串，供 [tools/audit.py](audit.md) 算哈希和 replay 对比。

## 17. 只读工具：read / ls / grep / glob（652–707 行）

四个执行器都是"薄封装 + 输出规范化"，共同点：**返回给模型的路径一律是工作区相对路径**。

`_read`（652–660 行）：`ws.read_text` 拿片段，输出 `f"{rel}\n{chunk}"`——**第一行是相对路径**。模型后面引用文件时能直接复制这一行。

`_ls`（663–667 行）：空目录返回 `"(empty)"` 而不是空字符串。空输出会让模型怀疑自己调错了。

`_grep`（670–691 行）：调 `grep_files(..., limit=DEFAULT_LIMIT, max_line=GREP_MAX_LINE_LENGTH)`，然后把命中行里的 `str(ws.root) + os.sep` 前缀剥掉——**rg 输出的是绝对路径，模型只需要相对路径**（省 token 且防模型照抄绝对路径）。无命中返回 `(no matches)`。

`_glob`（693–707 行）：目录不存在抛 `NotADirectoryError`；命中数达到 `limit` 时追加 `...[truncated]` 提示。它自己限制条数，所以 `truncate="none"`——**截断要在对的地方做**：glob 按条数截比按字节截有意义得多。

## 18. 写工具：`_guard_write` / write / edit（709–757 行）

**`_guard_write`（709–716 行）**：所有写路径的必经守卫。

```python
target = ws.resolve(raw)          # 解析 + 越界拦截（Workspace 干）
rel = ws.rel(target)
if is_sensitive_path(rel) or is_sensitive_path(str(target)):
    raise PermissionError(f"refusing to modify sensitive path {rel}")
```

**相对路径和绝对路径各查一遍**——防止 `.ssh/../foo` 这类绕过。这是继安全门之后的第二道防线（安全门在 `_prepare` 阶段也查了 `is_sensitive_path`），此处是执行前的最后把关。

**`_write`（718–725 行）**：`ws.write_text` 写文件，返回 `f"wrote {rel} ({n} lines)"` 加上 `_py_compile(path)` 的结果。**返回行数是个小设计**：模型据此知道自己写了多少，不用再 read 一遍确认。

**`_edit`（727–745 行）**：比 `write` 多三件事：

```python
if not path.exists(): raise FileNotFoundError(...)
original = path.read_text(encoding="utf-8")
newline = "\n" if original.endswith("\n") else ""
updated = _edit_replace(original, old, new, bool(args.get("replace_all")))
if not updated.endswith("\n") and newline: updated += "\n"   # ① 保留原换行风格
```

`newline` 那两行是**避免无意义的 diff 噪音**：原文件末尾有换行、替换后的内容没有，就补回去。否则每次 edit 都会在 diff 里留一个 "\ No newline at end of file"。

**`_edit_replace`（747–757 行）**：

```python
if old == "": raise ValueError("old_string must not be empty")
count = original.count(old)
if count == 0: raise ValueError("old_string not found; read the file again")
if count > 1 and not replace_all: raise ValueError(f"old_string matched {count} times; add context or set replace_all=true")
```

三条错误消息都是**指令式**的：0 次 → "read the file again"（告诉模型下一步）；多次 → "add context or set replace_all=true"（给出两个解法）。唯一性约束的意义是**edit 必须是可预测的**——模型指定 old → new 时，不能让它意外改到三个地方中的某一个。

## 19. 联网工具与 `py_compile` 快检（759–792 行）

`_web_search` / `_web_fetch`（759–775 行）：把 `WebError` 转成 `ValueError`——**统一异常类型**，让 `_run` 的宽 `except` 能一致处理成错误 `ToolResult`。（`WebError` 其实也是 `RuntimeError` 的子类，转成 `ValueError` 主要是为了语义：这是"参数/输入问题"，不是运行时故障。）

`_py_compile`（777–792 行）：写/编 `.py` 之后跑一次 `py_compile` 子进程（15 秒超时），失败就把 stderr 附在输出末尾。

```python
return f"\n\npy_compile failed:\n{err}"
```

**注意它不把语法错误标成 `is_error`。** 因为文件已经写成功了，工具调用本身没失败；语法错误只是附加信息，让模型下一轮自己修。这个区分很重要：`is_error=True` 会影响 [loop.py](../../../docs/loop-explained.md) 的 `changed_files` 记录和 UI 显示。

## 20. `_bash()`：前台执行与超时杀（794–851 行）

```python
proc = subprocess.Popen(command, shell=True, cwd=ws.root,
                        stdout=PIPE, stderr=PIPE, text=True, bufsize=1,
                        env={**os.environ, "PWD": str(ws.root)})
if background: return _start_job(proc, command, ws)
if on_proc: on_proc(proc)          # 记下进程供 abort_running
```

- `cwd=ws.root`：**沙箱的第一道边界**——命令默认在工作区内跑（配合安全门的路径越界检查）。
- `env` 里显式覆盖 `PWD`：光有 `cwd` 时 shell 的内建 `PWD` 变量可能不更新，某些脚本依赖它。
- `shell=True` 是必需的（要支持管道、重定向），代价是**安全门必须自己解析命令意图**（[tools/safety.py](safety.md) 的 `_segments` + `_parse_segment`）而不是靠参数数组。

**前台执行（821–851 行）**：

```python
def reader(stream, bucket):
    for line in iter(stream.readline, ""):
        bucket.append(line)
        if on_update: on_update(line)      # 逐行回调，UI 实时显示
```

**两个线程分别读 stdout 和 stderr**。这是必需的设计：如果只用一个线程读一个流，另一个流的 pipe 缓冲区写满（默认 64KB）后子进程会阻塞在 write 上，形成死锁（`communicate()` 内部就是这么做的）。

超时处理：

```python
try: proc.wait(timeout=timeout)
except subprocess.TimeoutExpired:
    _kill(proc); t_out.join(timeout=1); t_err.join(timeout=1)
    return _bash_result("timeout", stdout_parts, stderr_parts)   # 已读到的部分照样返回
except KeyboardInterrupt:
    _kill(proc); ...; raise                                       # 穿透
```

超时不是失败——`_bash_result("timeout", ...)` 把已收集的输出正常返回，模型能看到命令卡在哪一步。`join(timeout=1)` 而非无限等：读线程可能卡在 `readline`，不能让它拖住主流程。

**`_bash_result`（991–996 行）** 的格式是 `exit={code}\nstdout:\n{...}\nstderr:\n{...}`，空流写 `(empty)`。**`exit=` 打头**是为了配合第 12 节的 `keep_prefix` 逻辑。

## 21. `_start_job()`：后台作业的一生（853–899 行）

```python
job_id = f"job_{token_hex(4)}"
log_path = Path(ws.root) / ".wheel" / "outputs" / f"{job_id}.log"
handle = log_path.open("a", encoding="utf-8")
lock = threading.Lock()

def reader(stream):
    for line in iter(stream.readline, ""):
        with lock:
            handle.write(line); handle.flush()      # 两流共写一个文件，加锁
```

三个关键选择：

1. **输出落盘到 `.wheel/outputs/<job_id>.log`，而不是留在内存。** 因为作业跨回合存活，且 `_bash_result` 的保尾截断策略需要在多次 poll 之间累积完整输出。日志文件也让 `/expand` 之类的命令能拿到完整输出。
2. **stdout 和 stderr 合并进同一个文件**，用一把锁串行化。代价是分不清来源，收益是 `bash_poll` 一次读完、顺序正确（分别存两个文件的话，交错顺序就丢了）。
3. **每个流一个读线程 + 一个 `closer` 线程**（881–889 行）：`closer` 等 `proc.wait()` 返回后 join 两个读线程、关文件句柄。不这样收尾的话，进程退出时可能还有数据在 pipe 里没被读完，最后几行丢失。

返回值是给模型的指令：

```
background job_id=job_xxx pid=123 log=.wheel/outputs/job_xxx.log
Job keeps running after this turn. Tell the user this job_id and stop. Do not bash_poll in a loop now.
```

最后一句是**防止模型把 `bash_poll` 当成轮询循环**——每 poll 一次就消耗一整轮，任务还没跑完上下文先满了。

## 22. `bash_poll` / `bash_kill` / `_get_job`（901–938 行）

**`_bash_poll`（901–916 行）**：读日志文件全文 + 查进程状态。

```python
log = job.log_path.read_text(encoding="utf-8", errors="replace")   # errors="replace" 防半行 UTF-8
code = job.proc.poll()
status = f"status=running pid={...}" if code is None else f"status=exited code={code}"
return f"{status}\n{body}"
```

**poll 是非阻塞的**（`proc.poll()` 不等待），所以模型可以在一轮里调多个作业的 poll，也可以反复调同一个——每次读到的是累计输出。

`errors="replace"` 是必要的：写线程可能正写到一个多字节字符中间，此时读到的是不完整字节，严格解码会抛异常。

**`_bash_kill`（918–925 行）**：杀进程 / 或告知已退出。它在 [tools/safety.py](safety.md) 的 `OWN_JOBS` 集合里——只能杀自己的作业，不需要 y/N 确认。

**`_get_job`（927–938 行）**：精确匹配，失败时**前缀匹配且唯一命中**才认（`job_a1` 能匹配 `job_a1b2c3`）。前缀匹配是给 `/jobs kill job_a1` 这类手输场景用的；要求唯一命中避免歧义。

## 23. 作业表的外围接口（940–997 行）

| 函数 | 行号 | 谁用 | 职责 |
|---|---|---|---|
| `format_jobs` | 940 | `/jobs` | 列表文本：job_id + 状态 + 前 80 字符命令 |
| `kill_job` | 955 | `/jobs kill` | 转调 `_bash_kill` |
| `drain_job_events` | 959 | REPL 主循环 | 取走"作业退出"事件，置 `notified` 防重复 |
| `kill_all_jobs` | 972 | `atexit` | 杀光所有作业 |
| `_kill` | 982 | 内部 | `SIGKILL` + 等 2 秒，等不到就算了 |
| `_bash_result` | 991 | 内部 | 拼 `exit=` / stdout / stderr |

`_kill`（982–989 行）用的是 **SIGKILL 而非 SIGTERM**：agent 起的进程（测试、server）往往不处理 SIGTERM，且用户按 Ctrl+C 时期望立即停止。

```python
atexit.register(kill_all_jobs)   # 999 行
```

最后一行注册退出钩子——**不留孤儿进程**。dev server 忘关会占着端口很久，这是最常见的代价。

`drain_job_events`（959–970 行）返回 `f"job {id} exited {code}"` 列表，每个作业只报一次。UI 层把它作为普通事件排进输出流，不和正在输入的内容交错。
