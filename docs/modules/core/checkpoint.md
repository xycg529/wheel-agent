# `core/checkpoint.py` 逐段讲解

> 本篇讲文件级 undo 的快照存储。上游是 [tools/tools.md](../tools/tools.md)（`ToolRuntime` 在 write/edit/bash 前调它）和 [../../loop-explained.md](../../loop-explained.md)（第 3 节 `runtime.begin_task()`），下游是 [tools/safety.md](../tools/safety.md)（敏感路径判定）和 [ui/app-commands.md](../ui/app-commands.md)（`/undo`、`/undo-task`）。

改文件之前先把旧内容存一份，从而在不依赖 git 的前提下支持单步撤销和整任务回滚。

- 行数：201 行
- 依赖：[tools/safety.py](../tools/safety.md) — `is_sensitive_path()`，决定哪些文件不配拥有快照
- 被谁用：[tools/tools.py](../tools/tools.md)（`ToolRuntime._checkpoint()`，339、391、480、506 行）；[ui/app/commands.py](../ui/app-commands.md)（`handle_undo` / `handle_undo_task`，252、263 行）

## 目录

- [1. 导入与三个常量](#1-导入与三个常量1–20-行)（1–20 行）
- [2. 存储布局：`CheckpointStore` 与目录](#2-存储布局checkpointstore-与目录23–34-行)（23–34 行）
- [3. `begin_task` / `latest_task_id`：task_id 的生命周期](#3-begin_task--latest_task_idtask_id-的生命周期37–49-行)（37–49 行）
- [4. `snapshot()`：一份快照的内容与四个跳过条件](#4-snapshot一份快照的内容与四个跳过条件51–94-行)（51–94 行）
- [5. `snapshot_bash()`：尽力而为的命令行扫描](#5-snapshot_bash尽力而为的命令行扫描96–113-行)（96–113 行）
- [6. `rollback_task()`：整任务回滚（`/undo-task`）](#6-rollback_task整任务回滚undo-task115–141-行)（115–141 行）
- [7. `undo()`：单步撤销（`/undo [n]`）](#7-undo单步撤销undo-n143–160-行)（143–160 行）
- [8. 任务索引的读写与容错](#8-任务索引的读写与容错162–176-行)（162–176 行）
- [9. `_restore()`：三种恢复语义](#9-_restore三种恢复语义178–188-行)（178–188 行）
- [10. undo 栈的读写与 200 上限](#10-undo-栈的读写与-200-上限190–201-行)（190–201 行）

---

## 1. 导入与三个常量（1–20 行）

只依赖标准库加一个 `is_sensitive_path`。三个常量就是全部的策略旋钮：

| 常量 | 值 | 作用 |
|---|---|---|
| `MAX_BYTES` | 1_000_000 | 单文件快照上限，超过不存 |
| `SKIP_PARTS` | `.wheel` / `.wheel_runs` / `.git` | 路径里有这些**组件**就不存 |
| `_FLAG` | `^-` | bash 扫描时识别 `-rf` 这类选项，别当路径 |

`SKIP_PARTS` 的语义是"跳过工具自己的产物目录和 VCS 元数据"：快照目录本身在 `.wheel/checkpoints/`，不跳过就会自噬（回滚时把快照文件也回滚掉）。判定用 `any(part in SKIP_PARTS for part in path.parts)`（58 行），是**路径组件精确匹配**，不是子串包含——叫 `.wheel-foo` 的目录不会被误伤。

## 2. 存储布局：`CheckpointStore` 与目录（23–34 行）

```python
self._stack_path = self.dir / "stack.json"    # undo 栈：最近 200 个快照 ID
self._tasks_path = self.dir / "tasks.json"    # 任务 → 快照 ID 列表的映射
```

`for_workspace()`（33 行）把存储定在 `<工作区>/.wheel/checkpoints/`，并对工作区做 `resolve()`——**工作区用符号链接进入时也会落到真实路径下**，同一个仓库不会因为入口不同而产生两份快照库。

目录里三类文件：

```
.wheel/checkpoints/
  stack.json          ["<cid>", ...]                     全局 undo 栈（最新的在末尾）
  tasks.json          {"latest": "<task_id>", "items": {"<task_id>": ["<cid>", ...]}}
  <cid>.json          {"id","path","existed","content","tool"}   单个快照
```

**两份索引是刻意的分层**：`stack.json` 是跨任务的单一时间线，服务 `/undo`；`tasks.json` 按任务分组，服务 `/undo-task`。同一个 `cid` 同时出现在两处，各自维护，互不感知。

## 3. `begin_task` / `latest_task_id`：task_id 的生命周期（37–49 行）

```python
task_id = f"task_{int(time.time() * 1000)}_{token_hex(3)}"
tasks["latest"] = task_id
tasks.setdefault("items", {})[task_id] = []
```

**task = 一次 `run_agent` 调用。** [loop.py](../../loop-explained.md) 第 3 节（86 行）在组装组件时调 `runtime.begin_task()`，得到一个 id 存进 `RunResult.task_id`；`ToolRuntime.begin_task()`（tools.py 391–394 行）把它缓存进 `self._task_id`，之后本轮所有快照都带上这个 id（tools.py 513、515 行）。

所以一轮对话（可能几十次 write/edit）归入一个 task，`/undo-task` 一次全回滚；`/undo` 则无视任务边界，只看全局栈。

`latest_task_id()` 是 `/undo-task` 不带参数时的默认值（263 行）。

## 4. `snapshot()`：一份快照的内容与四个跳过条件（51–94 行）

调用时机是**改前**：`ToolRuntime._checkpoint()` 在 `spec.execute(...)` 之前调 `snapshot()`（tools.py 480 行），所以存的是"即将被覆盖的旧内容"。

快照存的是**文件完整内容，不是 diff**：

```python
rec = {"id": cid, "path": str(path), "existed": existed, "content": content, "tool": tool}
```

为什么是全量而非 diff：

- **恢复不需要依赖链。** diff 要按序正向应用才能回退，中间任一环缺失就断；全量副本任意一条都能独立恢复，`/undo` 才能做"弹栈顶一个"这种栈式操作。
- **新建文件天然表达。** 文件原本不存在时 `existed=False` + `content=None`，恢复动作就是"删掉它"（见第 9 节）。diff 形式没法表达"从无到有"。
- **代码量换存储。** 代价是磁盘：一个 100KB 的文件改 10 次就是 10 份副本。用下面四个跳过条件把代价压住。

四个跳过条件，全部静默返回 `None`（调用方 `_checkpoint` 也不看返回值，511–518 行还包了 `except Exception: return`——**快照失败绝不能阻塞主流程**）：

1. **路径含 `SKIP_PARTS` 或是敏感路径**（58 行）：`.wheel`/`.git` 产物目录，以及 `is_sensitive_path()` 判定的 `.env`、密钥、`.pem` 等。这类文件不快照，等于"改了就改了，撤销不了"——比把密钥明文写进快照目录安全。
2. **超过 1MB**（66–68 行）：全量副本会让 `.wheel/checkpoints` 暴涨，而大文件通常是生成物，回滚收益低。
3. **二进制**（74–76 行）：嗅探前 8KB 有没有 NUL 字节。

```python
if b"\0" in data[:8192]:
    return None
```

这一步是必须的：快照按 UTF-8 文本存取（`errors="replace"`），二进制文件走一遍 decode/encode 就被 `replace` 填坏了，恢复出来是损坏文件。**宁可不存，也不能存一个假的**。

4. **目录**（80 行）：`existed` 为 False 且 `path.exists()` 为真，说明是目录或特殊文件，直接跳过——快照只覆盖普通文件。

另外注意 55 行的 `path.resolve()`：**符号链接会被解析成目标真实路径**，快照记的是目标。好处是规避了"敏感路径通过软链绕过"的漏洞；代价是恢复时写回的是目标文件，链接本身不重建。

`content` 用 `read_bytes().decode("utf-8", errors="replace")`（78 行）——二进制读取保证不做换行转换，CRLF 文件的换行符原样保留。

快照 ID 是 `毫秒时间戳_token_hex(2)`，和 `begin_task` 同构，仅用于文件名和索引。

写完 `<cid>.json` 后做两件事（86–92 行）：追加进 undo 栈并**截断到最新 200 条**，以及挂到 `task_id` 名下（`task_id` 为 None 时跳过挂载，此时这个快照只能被 `/undo` 看到）。

## 5. `snapshot_bash()`：尽力而为的命令行扫描（96–113 行）

bash 工具没法"改前拦截"，只能在**执行前扫命令字符串**，猜出会被删/移的文件：

```python
if not re.search(r"\b(rm|mv)\b", command):
    return
for tok in command.split():
    if _FLAG.match(tok) or tok in {"rm", "mv", "sudo", "--"}:
        continue
    tok = tok.strip("\"'")
    if not tok or tok in {"*", ".", ".."}:
        continue
    path = resolve(tok)
    if path.is_file():
        self.snapshot(path, tool="bash", task_id=task_id)
```

- 只对含 `rm`/`mv` 的命令动手（99 行）——`cp`、`sed -i`、重定向覆盖都不在列。
- 跳过选项、命令名本身、引号、`*`/`.`/`..`（107–110 行）。
- `resolve` 由 `ToolRuntime` 传进来（`self.workspace.resolve`，tools.py 516 行），保证路径解析规则和安全门、写工具一致。
- 只对**确实是普通文件**的 token 存快照（113 行）。

已知限制（docstring 明说）：**只认字面量路径**。`rm -rf build/`（删目录）、`rm $FILES`（变量）、`rm *.log`（通配符展开）都覆盖不到。这是"尽力而为"——能救回一部分误删，不假装是完备的事务系统。

## 6. `rollback_task()`：整任务回滚（`/undo-task`）（115–141 行）

语义是**撤销一个任务内的全部文件改动**，不是单次改动：

```python
ids = list((tasks.get("items") or {}).get(task_id) or [])
for cid in reversed(ids):
    ...
    msgs.append(self._restore(rec))
    rec_path.unlink(missing_ok=True)
    if cid in stack:
        stack.remove(cid)
```

三个关键点：

1. **倒序遍历**（121 行）。同一文件在任务里被改多次会产生多个快照，只有**从最后一次改动往回倒**恢复，最终才会落到任务开始前的状态。正序恢复结果是"最后一次快照的内容"，等于没回滚。
2. **恢复后删快照文件并退栈**（126–128 行）。任务回滚后这些快照不再存在，`/undo` 不能再弹到它们——否则会出现"刚回滚完，/undo 又把中间态恢复回来"的错乱。
3. **`latest` 指针回退**（133–136 行）：

```python
if tasks.get("latest") == task_id:
    tasks["latest"] = next(reversed(items), None) if items else None
```

`items` 是 dict，Python 3.7+ 保序，倒序迭代取第一个即"次新的任务"。回滚完再敲 `/undo-task` 会接着回滚上一个任务，而不是重复回滚一个空 id。

返回的是每条文件的恢复消息列表（`restored X` / `removed X` / `skipped X`），由 `handle_undo_task` 逐条打印。

## 7. `undo()`：单步撤销（`/undo [n]`）（143–160 行）

比整任务回滚简单得多：从**全局栈**弹 n 次，逐个恢复、删快照文件，不管 task 归属。

```python
for _ in range(n):
    if not stack:
        break
    cid = stack.pop()
```

- `n = max(1, int(n))`（144 行）：`/undo 0`、`/undo -3` 都归一成撤一步，`/undo abc` 在命令层就被拦下（commands.py 246–249 行）。
- 栈空就提前 break，返回空列表 → UI 显示 `(nothing to undo)`。
- **和 `rollback_task` 的关键差异：不清理 `tasks.json`。** 见下面第 11 节第 2 条陷阱。

## 8. 任务索引的读写与容错（162–176 行）

`_load_tasks()` 的每一步都在防御（162–174 行）：

```python
if not self._tasks_path.is_file():
    return {"latest": None, "items": {}}
try:
    data = json.loads(...)
except (OSError, json.JSONDecodeError):
    return {"latest": None, "items": {}}
if not isinstance(data, dict):
    return {"latest": None, "items": {}}
data.setdefault("items", {})
```

文件缺失、半行截断（`_save_tasks` 非原子写，进程被杀会留下残 JSON）、类型不对，三种情况一律降级成空结构。**理由写在 docstring 里：回滚不能因为元数据坏了而崩溃**——元数据坏了最坏结果是"撤不了"，而抛异常会让 `/undo-task` 连带炸掉整个 TUI。

`_save_tasks()`（175 行）是 `write_text` 全量覆盖，没有锁、没有临时文件+rename。

## 9. `_restore()`：三种恢复语义（178–188 行）

```python
if rec.get("existed"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rec.get("content") or "", encoding="utf-8")
    return f"restored {path}"
if path.is_file():
    path.unlink()
    return f"removed {path}"
return f"skipped {path} (already gone)"
```

| 快照状态 | 含义 | 恢复动作 |
|---|---|---|
| `existed=True` | 文件改前存在 | 写回全文；父目录不存在就重建（处理"文件被整个目录删掉"的情况） |
| `existed=False` + 文件还在 | 这是本任务新建的 | 删除（`removed`） |
| `existed=False` + 文件已不在 | 用户或别的命令已经删了 | 跳过（`skipped ... already gone`） |

第二条就是 README 里说的"含新建的文件（删除）"。第三条让回滚**幂等友好**：重复 `/undo-task` 不会报错。

只恢复**内容和存在性**，不保存也不恢复 mtime、权限位、符号链接、扩展属性。

## 10. undo 栈的读写与 200 上限（190–201 行）

`_load_stack()`（190 行）和 `_load_tasks()` 同样的容错三连，损坏返回 `[]`。

上限在 `snapshot()` 的 88 行生效，不在 `_save_stack` 里：

```python
self._save_stack(stack[-200:])
```

栈深 200 是"够用"和"不涨"的折中：长会话里几千次编辑不能让 `stack.json` 无限增长，而人类几乎不会撤到 200 步之前。

**被截断淘汰的快照 `<cid>.json` 文件不会被删除**（88 行只改索引，不动目录），成为无人引用的孤儿。它们不占索引、不影响行为，但会一直留在 `.wheel/checkpoints/` 里占磁盘——清理只能手工删目录。

---
