# `tools/workspace.py` 逐段讲解

> 本篇讲工作区沙箱（Workspace）。上游是 [core/loop.md](../../loop-explained.md)（唯一构造它）和 [tools/tools.md](tools.md)（所有文件类工具经它访问磁盘），下游是 `pathlib`。

把 agent 的一切文件访问收口到一个对象：`root` 之内随便读写，解析后跳出 `root` 的路径一律拒绝。

- 行数：61 行
- 依赖：只有标准库 `pathlib.Path`——无第三方依赖，是全套工具的最底层
- 被谁用：
  - [core/loop.md](../../loop-explained.md) —— `run_agent` 里把用户给的 `workspace` 参数统一成 `Workspace` 实例
  - [tools/tools.md](tools.md) —— `ToolRuntime` 持有它，`read` / `ls` / `grep` / `glob` / `write` / `edit` / `bash` 全部通过它落盘
  - [tools/safety.md](safety.md) —— 另一套**独立**的路径越界判定（见下文"与 SafetyGate 的分工"）
  - [core/checkpoint.md](../core/checkpoint.md) —— 用 `workspace.root` 定位 `.wheel` 目录

## 目录

- [1. 模块定位与导入（1–6 行）](#1-模块定位与导入16-行)
- [2. `Workspace` 类与 `__init__`（9–14 行）](#2-workspace-类与-__init__914-行)
- [3. `resolve()`：越界拦截（16–24 行）](#3-resolve越界拦截1624-行)
- [4. 为什么不用裸 `Path`（设计说明）](#4-为什么不用裸-path设计说明)
- [5. 与 SafetyGate 的分工（设计说明）](#5-与-safetygate-的分工设计说明)
- [6. `rel()`：展示用相对路径（26–28 行）](#6-rel展示用相对路径2628-行)
- [7. `read_text()`：行级读取与编码容错（30–41 行）](#7-read_text行级读取与编码容错3041-行)
- [8. `write_text()`：自动建父目录（43–48 行）](#8-write_text自动建父目录4348-行)
- [9. `list_dir()`：目录优先排序（50–61 行）](#9-list_dir目录优先排序5061-行)

---

## 1. 模块定位与导入（1–6 行）

模块 docstring 一句话概括三层职责：**路径解析**、**越界拦截（沙箱）**、**读写列目录的统一行为**。

```python
from pathlib import Path
```

整个文件只 import 这一行。没有配置、没有事件总线、不认识 model——它是纯数据层，可以被任何地方 `new` 出来而不牵动别的状态。这也是它能当安全边界的前提：**边界代码越小越可信**。

## 2. `Workspace` 类与 `__init__`（9–14 行）

```python
def __init__(self, root: str | Path):
    self.root = Path(root).resolve()
    self.root.mkdir(parents=True, exist_ok=True)
```

两件事：

1. **`resolve()` 归一 root**：吃掉 `..`、`.`、符号链接和相对路径，得到绝对路径。后面所有越界判定都拿这个归一后的 root 做基准——如果 root 自己还带着 `..`，`relative_to` 的判断会失真。
2. **`mkdir(parents=True, exist_ok=True)`**：工作区不存在就建出来。agent 常被指向一个还没建的空目录（一次性任务 `--json` 场景），让它崩在这里没有意义。

类的唯一公开状态就是 `self.root` 一个字段。没有"当前目录"的概念——`bash` 工具的 cwd 永远是 `ws.root`（见 [tools/tools.md](tools.md)），所以 agent 无法通过 `cd` 挪动沙箱基准。

注意 root 是 `resolve()` 过的**真实路径**：如果工作区目录本身是通过符号链接进来的（比如 `~/src/wheel-agent` 里 `wheel_agent -> wheel-agent`），root 会指向链接的**目标**。这也意味着用户看到的路径和 `root` 可能字面不同，UI 显示时统一走第 6 节的 `rel()`。

## 3. `resolve()`：越界拦截（16–24 行）

```python
def resolve(self, path: str | Path) -> Path:
    raw = Path(path)
    candidate = raw.resolve() if raw.is_absolute() else (self.root / raw).resolve()
    try:
        candidate.relative_to(self.root)
    except ValueError as exc:
        raise PermissionError(f"path escapes workspace: {path}") from exc
    return candidate
```

这是整个沙箱的核心，三步：

1. **拼成绝对路径**：绝对路径直接用，相对路径挂到 `root` 下——所以 agent 传 `src/a.py` 和 `/abs/root/src/a.py` 在这里等价，模型不需要知道工作区在哪。
2. **`.resolve()` 再次归一**：先归一再判定，这样 `a/../../etc/passwd` 会被折成 `/etc/passwd` 再检查。**如果先做字符串匹配（比如判断 `..` 是否出现在路径里），就会漏掉 `a/../b/../../etc` 这类合法拼出来的逃逸**。用 `resolve()` 让 `pathlib` 去做折叠，是这里最省事也最可靠的写法。
3. **`relative_to` 当判定器**：能算出相对路径说明在 root 内；抛 `ValueError` 说明不在（比如 `/etc` 相对 `/home/u/proj` 算不出来）。这里**不返回 bool 而是直接抛 `PermissionError`**——越界是硬错误，没有"调用方自己决定要不要继续"的余地，忘了检查也不会漏。

抛出的是 `PermissionError` 而不是自定义异常：语义直白，且会被 [tools/tools.md](tools.md) 里 `ToolRuntime._run` 的 `except Exception` 兜住，转成 `ToolResult(is_error=True)` 回灌给模型——**沙箱拒绝不会炸掉主循环**，模型看到的是一行 `PermissionError: path escapes workspace: ../../etc/passwd`，它自己会改路径重试。

符号链接的处理值得一提：`resolve()` **跟随**链接。所以 workspace 内一个指向 `/etc` 的软链接，解析后会落在 root 外，被这里拒绝。这是偏保守的选择：宁可拒绝一个合法链接，也不放行一次逃逸。

## 4. 为什么不用裸 `Path`（设计说明）

直接在每个工具里写 `(root / path).resolve()` 也能跑，收口成 `Workspace` 换来四件事：

1. **检查点唯一**。越界判定只在这一处，改规则改一行。散在各工具里，迟早有一个工具忘了判。
2. **root 归一一次**。每个工具都 `Path(root).resolve()` 一遍既慢又容易不一致（有的归一了有的没归一，判定结果就会分歧）。
3. **`root` 被封装成不变量**。工具拿到的是 `Workspace` 对象而不是字符串路径，改不了 root，也绕不过检查——想访问磁盘就只有 `ws.read_text` / `ws.write_text` / `ws.list_dir` 三条路，每条都过 `resolve()`。
4. **给工具层一个稳定接口**。`_read` / `_ls` / `_write` 这些工具函数签名统一是 `(args, ws, on_update)`，它们不 import `pathlib`，换存储后端（内存文件系统、远程工作区）只改这个类。

代价是所有工具都拿到比裸 `Path` 更少的自由度（没法 `chmod`、没法建硬链接、没法 `stat` 拿 mtime）。这是有意的取舍：**工具层不该有这些能力**。

## 5. 与 SafetyGate 的分工（设计说明）

两套越界判定并存，容易看着像重复，其实是两层：

| | `Workspace.resolve()`（本文件） | `_resolve_target()`（[tools/safety.md](safety.md)） |
|---|---|---|
| 时机 | **执行时**（工具真要去读盘） | **裁决时**（工具还没跑，先问安不安全） |
| 输入 | 单个待访问的路径 | bash 命令里解析出的每一个字符串路径 |
| 手段 | `Path.resolve()` + `relative_to()`：真实文件系统 | 纯字符串 + `resolve()`：不要求文件存在 |
| 失败后果 | 抛 `PermissionError` → 转成工具错误回给模型 | 返回 `SafetyVerdict("deny", ...)` → 走 `blocked` 路径，进事件流的 `safety_decision` |
| 能否被绕过 | 不能（唯一的磁盘入口） | 能（它只是预检，绕过了还有 `resolve()` 兜底） |

为什么要两套：**safety 必须能判断尚不存在的文件**。`rm -rf build/` 里的 `build/` 可能还没建，`Path.resolve()` 对不存在的路径照样能算出绝对路径（不报错），但 safety 还要处理 `$HOME/x`、带盘符的 Windows 路径、命令里的 `~` 这些连 `Path` 都解析不了的字符串，所以它自己维护一套纯文本规则（见 [tools/safety.md](safety.md) 的 `_resolve_target`，404–432 行）。

反过来，`Workspace` **故意不 import safety**：它是纯数据层，不该知道"安不安全"这种策略问题。它的职责只有一条——**凡是经我手落盘的路径，必定在 root 内**。

一句话记：**safety 决定"要不要做"（allow / ask / deny），workspace 保证"做不了越界的事"。** safety 判 `allow` 的路径，`resolve()` 仍会再拦一道；safety 漏判的，靠 `resolve()` 兜底。

## 6. `rel()`：展示用相对路径（26–28 行）

```python
def rel(self, path: Path) -> str:
    return str(path.relative_to(self.root))
```

输入必须是**已解析的绝对路径**（通常是 `resolve()` 的返回值），输出 root 相对路径。

为什么值得单独一个方法：agent 和用户在终端里看到的应该是 `src/a.py`，不是 `/Users/<you>/Desktop/wheel-agent/src/a.py`。绝对路径既长又泄漏环境信息，而且**会污染上下文和事件流**——一旦绝对路径进了 `items`，同样的工作区换个机器跑，上下文指纹就对不上，[replay](../ui/replay.md) 的 `input_audit` 对比会误报"输入不一致"。所以工具输出、错误信息、事件字段一律走 `rel()`。

## 7. `read_text()`：行级读取与编码容错（30–41 行）

```python
def read_text(self, path: str, offset: int = 1, limit: int | None = None) -> tuple[str, int]:
```

签名里的两个细节：

- **`offset` 是 1-based**：和编辑器、`grep -n`、模型习惯的行号一致。`max(1, offset)` 把 0 和负数夹回 1——模型偶尔会传 `offset=0`，崩掉不划算。
- **返回 `(片段, 实际起始行号)`**：起始行号是**夹取之后**的值，不是入参原样。调用方（[tools/tools.md](tools.md) 的 `_read`）用它拼出给模型看的行号，模型拿到的行号和它接下来要 `edit` 的行号才对得上。

```python
lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
```

`errors="replace"` 是关键：agent 会去读第三方代码、日志、二进制文件，遇到非法 UTF-8 字节就抛 `UnicodeDecodeError` 的话，读一个 `.pyc` 就把工具搞挂了。替换成 `U+FFFD` 至少让模型看到内容并自己判断"这不是文本"。

`splitlines()` 而非 `readlines()`：丢掉行尾 `\n`，拼接时用 `"\n".join(...)`，避免行尾符在不同平台（CRLF）下混进输出。

两个前置检查各抛各的异常：`FileNotFoundError`（不存在）和 `IsADirectoryError`（传了目录）。分开是因为修法不同——前者模型该换个路径，后者模型该改用 `ls`。最后是切片：

```python
end = len(lines) if limit is None else min(len(lines), start - 1 + max(limit, 0))
chunk = "\n".join(lines[start - 1 : end])
```

`max(limit, 0)` 把负 limit 归零（返回空串而不是报错），`min(..., len(lines))` 让超限的 limit 退化成"读到末尾"。

## 8. `write_text()`：自动建父目录（43–48 行）

```python
target = self.resolve(path)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(content, encoding="utf-8")
```

`mkdir(parents=True)`：agent 写 `src/new/pkg/mod.py` 时中间目录往往还不存在。模型不该为了写文件先调两次 `bash mkdir`，所以这里默默建出来。

`encoding="utf-8"` 且**没有 `errors=`**：写入走严格模式。读可以容错（内容已经在那了），写不行——静默替换字符会产生一个看起来正常、实际已损坏的文件。

返回解析后的绝对路径，调用方（[tools/tools.md](tools.md) 的 `_write`）拿它去做 `ws.rel(path)` 拼输出、以及触发 `_py_compile` 语法检查。

**覆写没有任何护栏**：目标存在就直接盖掉。安全网不在这里，在 [core/checkpoint.md](../core/checkpoint.md)——`ToolRuntime._run` 在调 `spec.execute` 前先快照，靠 `/undo` 回退。这是"先做后审"的取舍：agent 改文件是高频操作，每次都问一次会打断流程，事后可撤销就够了。

## 9. `list_dir()`：目录优先排序（50–61 行）

```python
for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
    suffix = "/" if child.is_dir() else ""
    entries.append(self.rel(child) + suffix)
```

排序键 `(not p.is_dir(), p.name.lower())` 表达两件事：

- **目录排在前面**：`ls` 输出先给结构、再给文件，模型一眼看清布局。
- **名字用 `lower()` 比较**：大小写不敏感排序，`README.md` 不会因为它首字母大写而排在 `zoo.py` 后面。

`/` 后缀是有意的：**模型从这一条字符串就知道它是目录**，不用再调一次 `ls` 去试。省一轮工具调用，也省掉一轮误判。

两个非目录分支：

```python
if not target.exists():
    raise FileNotFoundError(...)
if not target.is_dir():
    return [self.rel(target)]
```

传了文件进来不报错，返回只含它自己的单元素列表——模型 `ls` 到文件时拿到的是一个正常的（虽然只有一个条目的）结果，而不是一条要它重试的错误。**能给出答案就别给错误**。
