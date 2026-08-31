# `core/events.py` 逐段讲解

> 本篇讲事件总线与运行记录的落盘/读取。上游是 [core/loop.py](../../../core/loop.py)（唯一的生产者）与 [ui/replay.py](../../../ui/replay.py)、[ui/app/commands.py](../../../ui/app/commands.py)（回放消费者），下游几乎没有依赖（只依赖标准库）。

一次运行的可观测性底座：把"发生了什么"写成一条扁平事件流并存盘，UI 渲染、审计、`/replay` 全部从同一份记录派生。

- 行数：150 行
- 依赖：无项目内依赖（只用 `json` / `hashlib` / `uuid` / `pathlib`）
- 被谁用：
  - [core/loop.py](../../../core/loop.py) —— 唯一的事件生产者，`emit` / `record_response` / `write_meta` 三个写入口都由它调用，见 [docs/loop-explained.md 第 19 节](../../loop-explained.md#19-eventbus一次-emit三处收益)
  - [core/session.py](session.md) —— 复用 `_now()` 和 `new_run_id()` 生成会话 ID 和时间戳
  - [ui/replay.py](../../../ui/replay.py) —— 读 `events.jsonl` 渲染时间线、读 `responses.jsonl` 当 `ScriptedModel` 的脚本
  - [ui/app/commands.py](../../../ui/app/commands.py) —— `/replay` 命令用 `load_run()` 找回运行
  - [ui/app/__init__.py](../../../ui/app/__init__.py) —— `/replay` 无参数时用 `list_run_ids()` 出选择列表

循环侧的视角（事件在循环里如何被发出、每种事件的字段）见 [docs/loop-explained.md 第 19 节](../../loop-explained.md#19-eventbus一次-emit三处收益)，本篇是模块级讲解，讲的是落盘格式、ID 规则与读取侧。

## 目录

- [1. 模块职责与两个概念](#1-模块职责与两个概念1–15-行)
- [2. `_now()`：统一时间源](#2-_now统一时间源18–20-行)
- [3. `new_run_id()`：运行 ID 规则](#3-new_run_id运行-id-规则23–26-行)
- [4. `EventBus` 数据模型与 `__post_init__`](#4-eventbus-数据模型与-__post_init__29–42-行)
- [5. `create()`：新建或复用运行目录](#5-create新建或复用运行目录44–48-行)
- [6. `subscribe()`：挂订阅者](#6-subscribe挂订阅者50–51-行)
- [7. `emit()`：先落盘、再喂订阅者](#7-emit先落盘再喂订阅者53–60-行)
- [8. `record_response()`：录制模型原始响应](#8-record_response录制模型原始响应62–80-行)
- [9. `write_meta()`：收尾元信息](#9-write_meta收尾元信息82–84-行)
- [10. `load_events()` / `load_responses()`](#10-load_events--load_responses86–92-行)
- [11. `_load_jsonl()`：容忍坏行](#11-_load_jsonl容忍坏行94–107-行)
- [12. `_json_hash()`：规范哈希](#12-_json_hash规范哈希109–112-行)
- [13. `list_run_ids()`：列出全部运行](#13-list_run_ids列出全部运行115–120-行)
- [14. `load_run()`：三种查找方式](#14-load_run三种查找方式123–150-行)

---

## 1. 模块职责与两个概念（1–15 行）

模块 docstring 点出两件事合在一处：**事件总线**（TTY 渲染 / JSONL / 审计同源）和**运行记录的读写**。之所以放一起，是因为它们本来就是同一件事——UI 看到的事件和落盘的事件是同一批 dict，不存在"UI 格式"和"日志格式"两套。

`Listener` 类型别名（14–15 行）定了订阅者的形状：收一个 `dict`，无返回。刻意不定义 `emit` 的返回语义给订阅者用，订阅者是纯粹的旁路。

一次运行对应一个 `run_dir`（默认在 `.wheel_runs/<run_id>/`，见 [core/config.md](config.md) 的 `WHEEL_RUNS_DIR`），里面三个文件：

| 文件 | 谁写 | 何时写 | 内容 |
|---|---|---|---|
| `events.jsonl` | `emit()`（53–60 行） | 每个事件即时追加 | 全部事件，一条一行，含 `type` / `run_id` / `ts` |
| `responses.jsonl` | `record_response()`（62–80 行） | 每次模型调用返回后 | 每轮的原始 `output` + `usage` + `output_sha256` + `input_audit` |
| `meta.json` | `write_meta()`（82–84 行） | run 收尾时一次写完 | provider / 模型 / token / 指纹 / 改动 / `session_id` |

三者分工明确：`events.jsonl` 是**过程**，`responses.jsonl` 是**模型的原始回答**（重放的输入），`meta.json` 是**结论与重跑配置**。

## 2. `_now()`：统一时间源（18–20 行）

本地时区 ISO 时间戳。用 `astimezone()` 而不是 `utcnow()`，是为了日志时间能直接和人读的本地时间对上——`events.jsonl` 主要是给人看和排序的，不是给分布式系统做因果排序的。

## 3. `new_run_id()`：运行 ID 规则（23–26 行）

```python
stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
return f"{stamp}_{uuid.uuid4().hex[:8]}"
```

时间戳精确到秒，所以**同一秒内并发**靠 8 位随机十六进制后缀区分（约 43 亿分之一碰撞）。为什么不让时间戳更细（比如带毫秒）？因为 ID 要当目录名、要在终端里被手打（`/replay 20250115T143022_a3f9c1b2`）——秒级可读性比理论唯一性值钱。

排序性质很关键：ID 字典序 = 时间序，所以 `list_run_ids()`（115–120 行）可以直接按目录名倒序排。

同一个函数也被 [core/session.py](session.md) 借去生成会话 ID（第 89 行）——会话 ID 和运行 ID 用同一套规则，方便在 `meta.json` 里混着查。

## 4. `EventBus` 数据模型与 `__post_init__`（29–42 行）

`@dataclass` 只有三个字段：`run_id`、`run_dir`、`listeners`。`listeners` 用 `field(default_factory=list)`，避免 dataclass 的可变默认值陷阱。

`__post_init__`（37–42 行）做两件事：建目录、定义三个文件路径。**路径在构造时就定死**，之后所有读写都走这三个属性，`load_run()`（123–150 行）才能把已有的目录直接包成一个可用的 `EventBus`——读侧和写侧是同一个类，没有单独的 `RunReader`。

注意这里没有锁：事件总线假设单线程写入（主循环一条线）。订阅者是同步调用的，跑在发事件的线程上。

## 5. `create()`：新建或复用运行目录（44–48 行）

`run_id=None` 时自动生成，传了就复用同名目录。后者的用途是**回放**：`/replay` 重跑时 `run_agent` 会新开一个 run（新 ID），但 `load_run()` 用录制的 ID 打开的是原目录——所以"复用目录"这条路径实际是给测试和显式指定的场景留的。

`runs_dir` 由 [core/config.md](config.md) 的 `AgentConfig.runs_dir` 提供，默认 `.wheel_runs`。

## 6. `subscribe()`：挂订阅者（50–51 行）

一行 `append`。没有退订接口——订阅者的生命周期跟随 bus 本身（一次运行）。

[core/loop.py](../../../core/loop.py) 第 74–75 行把 `on_event` 回调挂上来：

```python
if on_event:
    bus.subscribe(on_event)
```

这就是"UI 只是事件流的一个视图"的全部实现：循环不认识 UI，UI 自己贴上来。

## 7. `emit()`：先落盘、再喂订阅者（53–60 行）

```python
event = {"type": type_, "run_id": self.run_id, "ts": _now(), **data}
with self.events_path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
for listener in list(self.listeners):
    listener(event)
return event
```

三个设计点：

**顺序：先落盘再喂订阅者。** 落盘先完成，意味着订阅者抛异常不会丢事件；反过来若先渲染，UI 崩了日志就断了。审计数据比渲染重要。

**`list(self.listeners)` 副本遍历。** 遍历快照而不是原列表，订阅者在回调里增删 `listeners` 不会触发 `RuntimeError: list changed size during iteration`。

**每次 `open("a")` 而不是持有句柄。** 追加模式每次打开即写即关，进程被 `SIGKILL` 时最多丢最后一行的部分内容，不会有未 flush 的缓冲区。代价是每个事件一次 `open` 系统调用——事件量是每轮几条，完全可以接受。这和 [core/session.py](session.md) 的 flush + fsync 是同一套"崩溃安全优先"取舍。

`ensure_ascii=False`：中文内容原样进日志，人可以直接 `cat` 读。

`**data` 放在最后：调用方传的字段会覆盖 `type` / `run_id` / `ts` 三个保留键。这是个刻意的后门还是疏漏难说，但发事件时不要用这三个名字。

返回值是构造好的 `event` dict——调用方偶尔要拿 `ts` 或生成的完整事件。

## 8. `record_response()`：录制模型原始响应（62–80 行）

```python
row = {
    "turn": turn,
    "output": output,
    "usage": usage or {},
    "output_sha256": _json_hash(output),   # 响应哈希：replay 对比时校验录制未变
}
if input_audit:
    row["input_audit"] = input_audit
```

这是 `/replay` 能离网重跑的**唯一数据源**。两个字段撑起对比语义：

- **`output`（模型原始响应）**：直接当 `ScriptedModel` 的脚本。`ui/replay.py` 的 `recorded_scripts()`（60–62 行）就是 `[row["output"] for row in bus.load_responses()]`——按顺序依次返回录制好的响应，模型客户端完全不发 API。
- **`input_audit`**：这一轮发请求时的上下文指纹（由 [core/loop.py](../../../core/loop.py) 用 [tools/audit.py](../../../tools/audit.py) 的 `item_audit()` 算）。重跑时同样算一遍，两者相等说明"喂给模型的输入一致"，工具序列才有可比性。见 `ui/replay.py` 的 `_input_signature()`（87–89 行）和 `_replay_status()`。

`output_sha256` 是给录制内容做的校验和：同样内容的响应一定得到同样哈希（靠 `_json_hash` 的键排序，见第 12 节）。有意思的是当前 `ui/replay.py` 并未读取它——它是留给"校验录制文件是否被篡改/截断"的审计字段，读侧还没用上。

`record_response` 不走 `emit`：它是**按 turn 的录制**，不是事件流的一部分，所以独立成 `responses.jsonl` 而非混进 `events.jsonl`。

## 9. `write_meta()`：收尾元信息（82–84 行）

一次性覆盖写（`write_text`），带 `indent=2`——`meta.json` 是给人读和给程序读两用的。内容由 [core/loop.py](../../../core/loop.py) 攒的 `meta` 字典传入（第 336–358 行）：workspace、task、task_id、两个工作区指纹、环境指纹、provider/base_url/api/model、effort 三件套、turns、stop_reason、三类 token、tool_calls / tool_errors。

两个下游用途：

- **重跑配置来源**：`ui/replay.py` 的 `replay_run()`（123 行起，读 meta 在 134–150 行）从 `meta.json` 里读 `provider` / `model` / `task` / `turns` 来构造 `ProviderConfig` 和 `AgentConfig`，`api_key=""`——录制的响应不需要真 key。
- **`session_id` 关联**：`ui/app/__init__.py` 第 239 / 272 行通过 `extra_meta={"session_id": ...}` 把会话 ID 塞进来，`load_run()` 的第三种查找和 `ui/graph.py` 的 `list_session_runs()`（629 行）都靠它把 run 归到会话名下。

## 10. `load_events()` / `load_responses()`（86–92 行）

两个对称的读方法，都是"文件不存在就返回空列表"——**没有元数据的空运行**是合法状态（比如 `run_agent` 一开始就崩了）。

## 11. `_load_jsonl()`：容忍坏行（94–107 行）

```python
for line in path.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(row, dict):
        out.append(row)
```

三重过滤：空白行、非法 JSON、非 dict 行，全部**静默跳过**。

为什么必须容忍坏行：事件是逐行追加的，进程被强杀时正在写的那一行会留下**半行**。如果读取端严格解析，一次崩溃就会让整个运行记录永久不可读——这恰恰是最需要看日志的时候。JSONL 的行粒度追加 + 读侧跳过坏行，组合出"崩溃最多损失最后一行"的性质。这和 [core/session.py](session.md) 的会话文件用的是同一套思路（README 里也点明了这一点）。

用 `read_text().splitlines()` 一次性读全文而非逐行迭代：运行记录是 MB 级以下的小文件，简单性优先。

## 12. `_json_hash()`：规范哈希（109–112 行）

```python
raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

关键在于 `sort_keys=True` 和紧凑分隔符：Python dict 保序，同一个逻辑内容如果字段顺序不同，`json.dumps` 会给出不同字符串。规范序列化保证**同样内容一定得到同样哈希**，`output_sha256` 才能作为内容校验和用。

## 13. `list_run_ids()`：列出全部运行（115–120 行）

列出 `runs_dir` 下的所有目录名，倒序（新的在前）。目录不存在返回空列表而不是抛错——首次使用还没跑过任何任务是常态。

用途：`/replay` 不带参数时给一个 ↑↓ 选择列表（`ui/app/__init__.py` 第 634–641 行）。

## 14. `load_run()`：三种查找方式（123–150 行）

按 ID 找回运行，**三级回退**：

**① 精确 ID**（125–127 行）：`runs_dir/<run_id>` 存在就直接返回。

**② 前缀匹配**（128–132 行）：

```python
matches = sorted(root.glob(f"{run_id}*"), key=lambda p: p.stat().st_mtime, reverse=True)
dirs = [p for p in matches if p.is_dir()]
if len(dirs) == 1:
    return EventBus(run_id=dirs[0].name, run_dir=dirs[0])
```

只在前缀唯一命中时才采用——多个匹配就继续往下走，不做"猜一个"的事。按修改时间排序意味着即使有朝一日 ID 规则变了也能工作。

**③ 按 `session_id` 查**（133–147 行）：遍历所有运行目录，读各自的 `meta.json`，比对 `session_id` 是否相等或以 `run_id` 开头。多个命中时取**最近修改**的那个。

这一级的存在理由：用户手头有的通常是会话 ID（`/sessions`、`/tree` 显示的都是它），而不是 run ID。`ui/app/commands.py` 第 63–66 行专门写了提示文案：

```
hint: /replay wants a .wheel_runs id, not a session id; session ids work if a run recorded session_id
```

——即"会话 ID 也能用，前提是那次运行记录了 `session_id`"。注意这个遍历是 O(运行数 × 读 meta.json)，运行目录堆了成千上万个时会明显变慢。

**找不到就抛 `FileNotFoundError`**（149 行），由命令层捕获并转成一行红色提示（`ui/app/commands.py` 第 63–66 行）。

`ui/app/commands.py` 第 67–68 行还有一处细节：

```python
if bus.run_id != run_id:
    print(style.dim(f"resolved {run_id} → run {bus.run_id}"))
```

前缀/会话查找成功后告诉用户实际解析到了哪个 run——避免"我输的是 abc，它跑的是别的"这种困惑。
