# `tools/audit.py` 逐段讲解

> 本篇讲审计与指纹层。上游是 [core/loop.py](../../../core/loop.py)（每轮和每个工具批次调用它）和 [ui/graph.py](../../../ui/graph.py)（渲染时脱敏），下游是 [tools/safety.py](safety.md) 的 `is_sensitive_path()`。

把输入、输出、工作区、环境都压成**可移植的稳定哈希**，让 replay 能判定"两次运行是否等价"，同时保证密钥类内容不进日志和事件流。

- 行数：148 行
- 依赖：
  - [tools/safety.py](safety.md) —— `is_sensitive_path()`，敏感路径判定（唯一外部依赖）
  - 标准库 `hashlib` / `json` / `os` / `platform` / `sys` / `pathlib`
- 被谁用：
  - [core/loop.py](../../../core/loop.py) —— 三处：`agent_start` 拍环境+工作区指纹、`turn_start` 算 `input_audit`、`_run_tools`（第 18 节"审计三明治"）拍前后快照与脱敏
  - [ui/graph.py](../../../ui/graph.py) —— 渲染会话树时脱敏工具参数与结果
  - [ui/replay.py](../../../ui/replay.py) —— 消费 `input_audit` / 工作区指纹判定 `exact` / `behavioral` / `drift`
  - [docs/loop-explained.md](../../loop-explained.md) 第 18、19 节 —— 这两个概念的使用场景

## 目录

- [1. 模块定位与 `SKIP_PARTS`（1–17 行）](#1-模块定位与-skip_parts1–17-行)
- [2. `canonical` 与 `sha256_value`（19–26 行）](#2-canonical-与-sha256_value19–26-行)
- [3. `_normalize_for_audit`（28–48 行）](#3-_normalize_for_audit28–48-行)
- [4. `item_audit`（50–58 行）](#4-item_audit50–58-行)
- [5. 环境快照与环境指纹（60–73 行）](#5-环境快照与环境指纹60–73-行)
- [6. `workspace_manifest`（76–100 行）](#6-workspace_manifest76–100-行)
- [7. `workspace_fingerprint` 与 `workspace_changes`（103–116 行）](#7-workspace_fingerprint-与-workspace_changes103–116-行)
- [8. `redact_tool_args` / `redact_tool_output`（119–134 行）](#8-redact_tool_args--redact_tool_output119–134-行)
- [9. `tool_audit`（137–148 行）](#9-tool_audit137–148-行)

---

## 1. 模块定位与 `SKIP_PARTS`（1–17 行）

模块 docstring 点出整个文件的核心技巧：

> 关键是把 workspace 绝对路径替换成 `<workspace>` 占位符，使指纹可移植。

绝对路径进哈希会导致同一个任务换个目录跑、指纹就变，replay 判定必然失真。所以下面 `_normalize_for_audit()` 是全局最关键的 15 行。

```python
SKIP_PARTS = {".wheel", ".wheel_runs", ".git", "__pycache__"}
```

生成工作区清单时跳过的目录名（按路径**片段**匹配，任意层级命中即跳过）：

| 片段 | 为什么跳过 |
|---|---|
| `.wheel` | 工具自己的状态目录（会话文件、undo 备份、checkpoint） |
| `.wheel_runs` | 事件流落盘目录（[core/events.py](../core/events.md)）——**不跳过会让"跑一次 run"本身变成工作区变更**，diff 永远脏 |
| `.git` | VCS 元数据，与任务内容无关 |
| `__pycache__` | Python 字节码缓存，随运行产生 |

注意这里**没有** `node_modules` / `.venv` / `target` / `dist`。在有依赖目录的工作区里，清单会递归扫进去，体积和耗时都可能很大（见第 10 节）。

## 2. `canonical` 与 `sha256_value`（19–26 行）

```python
def canonical(value):   return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def sha256_value(value): return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
```

整个模块的哈希底座，两个函数共 8 行，但决定了"指纹稳定"这件事成不成立：

- `sort_keys=True` —— dict 键顺序不影响结果。模型返回的 item 字段顺序可能随 provider 变化。
- `separators=(",", ":")` —— 紧凑分隔符，去掉默认 `", "` / `": "` 的空格，减少无意义差异。
- `ensure_ascii=False` —— 保留中文原样（配合显式 `encode("utf-8")`），避免转义形式带来的长度膨胀。

`sha256_value()` 接受任意可 JSON 化的值，所以下面 list、dict、str 都走同一个入口。

## 3. `_normalize_for_audit`（28–48 行）

```python
if isinstance(value, str) and workspace:
    root = str(Path(workspace).resolve())
    out = value.replace(root, "<workspace>")
    raw = str(workspace)
    if raw != root:
        out = out.replace(raw, "<workspace>")
    return out
```

递归遍历 dict / list，把所有字符串里的 workspace 绝对路径换成 `<workspace>`。**替换两次**（解析后的 `root` 和传入的原始串 `raw`）是有原因的，注释里写清楚了：macOS 上 `/tmp` 是 `/private/tmp` 的符号链接，只替换 `resolve()` 后的路径，未解析的写法会逃过替换。

设计上的取舍：

- **只替换 workspace 前缀**，其他绝对路径（`/tmp/xxx`、`/etc/hostname`、`/Users/alice/...`）原样保留。所以上下文里若含机器相关的临时路径，replay 换机器就会判定输入不一致——这是可接受的保守选择：宁可报 `behavioral`，也不要把不同的输入误判成 `exact`。
- 替换是**朴素子串替换**，不做路径边界检查。极端情况下工作区根恰好是另一个字符串的片段时会误替换，实际影响极小。

## 4. `item_audit`（50–58 行）

```python
def item_audit(items, workspace=None):
    normalized = _normalize_for_audit(items, workspace)
    return {
        "sha256": sha256_value(normalized),
        "count": len(items),
        "types": [str(item.get("type") or item.get("role") or "") for item in items],
    }
```

一轮模型请求的**上下文指纹**。三个字段各有分工：

- `sha256` —— 强判定，内容级一致。
- `count` —— 弱判定，条目数。
- `types` —— 结构级判定：哈希不等时，靠它能看出是"条目数变了"还是"条目类型序列变了"（比如多了一轮 tool 输出）。取 `type or role` 是因为 item 有两种形态：OpenAI 的 `function_call_output` 等带 `type`，而最原始的 user 消息只有 `role`。

**为什么它能判定 replay 的"输入一致"**：[core/loop.py](../../../core/loop.py) 在每轮 `turn_start` 前调 `item_audit(request_items, ws.root)`，并通过 `bus.record_response(..., input_audit=...)` 写进 `responses.jsonl`。[ui/replay.py](../../../ui/replay.py) 的 `_input_signature()` 把两次运行的 `input_audit` 序列拉出来直接比相等（`inputs_same`），作为判定 `exact` / `behavioral` 的三要素之一（另两个是工具调用序列和 `stop_reason`）。

有一个细节：哈希算的是**规范化前**的 `items` 还是规范化后？是规范化后（`normalized`）—— 所以工作区路径被抹平了，同一段对话在不同目录下跑能得到同一个指纹，这才是可移植的含义。

## 5. 环境快照与环境指纹（60–73 行）

```python
def environment_snapshot():
    return {
        "python": platform.python_version(),     # 3.12.4
        "platform": platform.platform(),         # macOS-14.5-arm64-...
        "machine": platform.machine(),           # arm64
        "executable": os.path.basename(sys.executable),   # python3.12
        "shell": os.path.basename(os.environ.get("SHELL") or ""),   # zsh
    }
```

五个维度：Python 版本、平台串、CPU 架构、解释器名、默认 shell。都是**取 basename 而非完整路径**（`executable`、`shell`）——完整路径含用户名和工作区位置，会破坏可移植性。

`environment_fingerprint()` 就是 `sha256_value(environment_snapshot())`：事件流里放紧凑的哈希，需要人读时再取完整快照。

设计意图：环境差异（比如从 Python 3.11 换成 3.12、从 x86 换 arm64）会让 agent 行为漂移，录一个指纹就能在事后发现"哦，这次重跑环境不一样"。不过见第 10 节——目前 replay 判定并没有真正用它。

## 6. `workspace_manifest`（76–100 行）

```python
for path in sorted(root.rglob("*")):
    rel = path.relative_to(root)
    if any(part in SKIP_PARTS for part in rel.parts): continue
    if is_sensitive_path(rel_text): continue
    if path.is_symlink():  result[rel_text] = "symlink:" + os.readlink(path)
    elif path.is_file():   result[rel_text] = f"file:{path.stat().st_size}:{digest}"
```

**清单不是路径列表，而是 `{相对路径: 内容描述}` 字典**，值分三种形态：

| 值形态 | 含义 |
|---|---|
| `file:<size>:<sha256>` | 普通文件，内容哈希（`path.read_bytes()` 全量读取） |
| `symlink:<target>` | 符号链接，记目标路径（不跟随） |
| `unreadable` | `OSError` / `UnicodeError` 兜底（权限、坏链接、解码失败） |

几点设计取舍：

- **用内容哈希而非 mtime/size**：`cp -r` 或 `git checkout` 会改 mtime 但不改内容，用 mtime 会把"没实际变化"报成 modified。代价是要读全量文件。
- **只记文件不记目录**：目录本身没有内容，其存在性由子文件路径隐含表达。空目录不可见——可接受，因为 diff 关心的是文件内容。
- **敏感路径直接跳过**（`is_sensitive_path`），连条目都不留。所以密钥文件的增删改在 `workspace_changes` 里完全不可见——这是刻意的（见第 10 节）。
- `sorted(root.rglob("*"))` 排序后再遍历，保证清单构建顺序稳定（虽然最终指纹走 `sort_keys`，顺序不影响哈希，但便于人读和调试）。
- 路径统一 `as_posix()`（正斜杠），跨平台一致。

## 7. `workspace_fingerprint` 与 `workspace_changes`（103–116 行）

指纹就是清单的 `sha256_value()`——一次调用前后各拍一次，指纹相等即"工作区没变"。

`workspace_changes()` 是标准的三路集合 diff：

```python
"added":    sorted(after_keys - before_keys),
"deleted":  sorted(before_keys - after_keys),
"modified": sorted(k for k in before_keys & after_keys if before[k] != after[k]),
```

输出三个**排序后的**列表（排序保证同样结果字符串相同，可直接进事件流做相等比较）。注意 `modified` 的判定是比较**值字符串**，也就是 `file:size:sha256` 整体——大小或内容任一变化都算 modified。

在 [core/loop.py](../../../core/loop.py) 里的两处用法：

- **run 级**：`initial_manifest`（第 87 行）和 `final_manifest`（第 320 行）对比，得出整次 run 的 `workspace_changes`，进 `agent_end` 事件和 `meta.json`。
- **工具批级**：`_run_tools` 的 `before` / `after`（第 439、453 行）对比，得出"这批工具改了哪些文件"，进每个 `tool_execution_end` 事件。

## 8. `redact_tool_args` / `redact_tool_output`（119–134 行）

两个函数的脱敏判定都复用 [tools/safety.py](safety.md) 的 `is_sensitive_path()`（第 117–132 行），它认这些模式：

- 路径片段含 `.git`
- 文件名 ∈ `KEY_NAMES`（`id_rsa` / `id_ed25519` / `id_ecdsa` / `credentials.json` / `auth.json` / `secrets.json`）
- 后缀 `.pem` / `.p12` / `.pfx` / `.key`
- 名为 `.env`，或 `.env.*` 且不在 `ENV_ALLOW`（`.env.example` / `.env.sample`）

**`redact_tool_args`**（参数侧）：只处理 `write` / `edit`，且只对敏感路径生效——把 `content` / `old_string` / `new_string` 三个字段换成 `"<redacted>"`。路径本身保留（知道"它想改 `.env`"是有价值的审计信息），只抹掉内容。

**`redact_tool_output`**（输出侧）：`read` / `web_fetch`，敏感路径则整段替换成 `"<redacted sensitive tool output>"`。这里比参数侧更狠——读一个密钥文件，返回的就是明文内容，没有"部分字段"可以挑，只能整段抹掉。

判定用的 key 取值是 `args.get("path") or args.get("url")`：对 `read` 取 `path`，对 `web_fetch` 取 `url`。（见第 10 节关于 `web_fetch` 的实际效果。）

调用点：[core/loop.py](../../../core/loop.py) 第 448、465 行（`tool_execution_start` / `tool_execution_end` 事件），[ui/graph.py](../../../ui/graph.py) 第 237、239 行（渲染工具节点）。

## 9. `tool_audit`（137–148 行）

```python
def tool_audit(call, result):
    return {
        "tool_call_id": str(call.call_id),
        "tool_name": str(call.name),
        "args_sha256": sha256_value(call.arguments),
        "decision": getattr(result, "safety_decision", "") or "",
        "decision_reason": getattr(result, "safety_reason", ""),
        "decision_source": getattr(result, "safety_source", ""),
        "is_error": bool(result.is_error),
        "blocked": bool(result.blocked),
    }
```

一次工具调用的审计记录，八个字段来源分三类：

- **调用标识**：`call_id` / `name`，来自 `FunctionCall`([core/types.py](../core/types.md))。
- **参数哈希**：`sha256_value(call.arguments)`。**注意这里没有做 workspace 规范化**——和 `item_audit` 不同，工具参数的指纹是"原始参数"的哈希，换了工作区路径就会变。这是刻意的：工具参数是 agent 的实际动作，路径不同就是动作不同。
- **安全裁决**：`decision` / `decision_reason` / `decision_source` 全部用 `getattr(result, ...)` 从 `ToolResult` 上取，来自 [tools/safety.py](safety.md) 的 `SafetyGate` 裁决（`allow` / `ask` / `deny` + 理由 + 来源：`rules` / `memory` / `user`）。用 `getattr` 带默认值是为了能接受任何鸭子类型的 result—— loop.py 第 449 行就传了个临时构造的 `ToolResult(call_id, name, "")`，那时裁决字段还是空的。

`args_sha256` 的两处用法（都在 [core/loop.py](../../../core/loop.py) 的 `_run_tools` 里）：

1. **执行前**（第 449 行）：用 `tool_audit(call, ToolResult(call.call_id, call.name, ""))["args_sha256"]` 取哈希发 `tool_execution_start`。此时结果还不存在，所以造一个空 `ToolResult` 只为拿到哈希字段。
2. **执行后**（第 457 行）：完整 `tool_audit(call, result)`，八个字段全填，供 `tool_execution_end` 用。

[ui/replay.py](../../../ui/replay.py) 的 `_tool_signature()` 把 `tool_execution_start` 的 `tool_name` + `args` 和 `tool_execution_end` 的 `decision` 拼成序列，两次运行对比得出 `tools_same`。

---
