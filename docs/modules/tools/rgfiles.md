# `tools/rgfiles.py` 逐段讲解

> 本篇讲文件搜索。上游是 [tools/tools.py](tools.md) 的 `glob` / `grep` 工具，下游是 [tools/tools.md](tools.md) 的工具注册表和返回封装。

一句话职责：按文件名找路径（glob）、按内容找行（grep）；优先用系统 ripgrep，没有 rg 或它失败时退回纯 Python 的 `os.walk` 实现。

- 行数：243 行
- 依赖：标准库（`os` / `re` / `shutil` / `subprocess` / `functools.lru_cache`），无项目内 import
- 被谁用：[tools/tools.py](tools.md)（`glob_files` 喂给 glob 工具、`grep_files` 喂给 grep 工具）
- 与 [tools/atfiles.py](atfiles.md) 的分工：**rgfiles 是「模型主动搜索」**（输入是 pattern，返回命中列表），**atfiles 是「用户输入 @ 时的路径补全」**（输入是前缀，返回候选路径）。一个面向模型的上下文，一个面向用户的输入框。

## 目录

- [1. 常量：跳过目录与默认上限](#1-常量跳过目录与默认上限-14-42-行)
- [2. `expand_braces`：花括号展开](#2-expand_braces花括号展开-45-56-行)
- [3. `rg_bin` 与双实现切换](#3-rg_bin-与双实现切换-59-95-行)
- [4. `grep_files`：搜内容](#4-grep_files搜内容-97-131-行)
- [5. `_name_matches` 与 glob 匹配](#5-_name_matches-与-glob-匹配-134-176-行)
- [6. `_glob_walk`：纯 Python 回退](#6-_glob_walk纯-python-回退-179-199-行)
- [7. `_grep_walk`：纯 Python 回退](#7-_grep_walk纯-python-回退-201-243-行)

---

## 1. 常量：跳过目录与默认上限（14–42 行）

```python
SKIP_DIRS = {".git", ".venv", "__pycache__", ".wheel_runs", ".wheel", ".pytest_cache",
             "node_modules", "build", "dist", "target", ".gradle", "generated"}
IGNORE_GLOBS = ("!.git/**", "!.venv/**", ...)   # 同集合的 rg glob 写法
DEFAULT_LIMIT = 200   # 默认最多返回 200 个结果，防大仓库打爆上下文
```

`SKIP_DIRS` 和 `IGNORE_GLOBS` 是**同一份跳过名单的两种写法**：前者给纯 Python 的 `os.walk` 用（目录名集合），后者给 rg 用（带 `!` 的排除 glob）。两套实现共用同一语义，保证「有 rg 和没 rg 搜出来的东西一致」。

`DEFAULT_LIMIT = 200`：搜索结果的条数上限。注释写明理由——防大仓库打爆上下文。200 是经验值，不是算出来的：一条 grep 命中约 50–100 token，200 条约 20k token，是「够用但不吞掉半条上下文」的量。超过时追加 `...[truncated]` 提示模型「还有更多」，模型可换更精确的 pattern 再搜。

## 2. `expand_braces`：花括号展开（45–56 行）

```python
def expand_braces(pattern: str) -> list[str]:
    # 展开一层 bash 风格的 {a,b}，让 rg -g 能看到每个备选（rg 不展开花括号）
```

rg 的 `-g` glob 参数**不展开** `{a,b}` 花括号，但用户习惯写 `*.py{,x}` 这种 bash 风格。这个函数递归展开：找第一个 `{...}`，按 `,` 拆成多份，每份递归再展开。`*.py{,x}` → `["*.py", "*.pyx"]`。

只展开**一层**语义（外层展开，内层递归处理嵌套），不处理 `?` 可选、`{a..b}` 范围这类 bash 高级写法——超出部分按字面传给 rg，rg 不认就匹配不到，模型看到空结果会自己换写法。

## 3. `rg_bin` 与双实现切换（59–95 行）

```python
def rg_bin() -> str | None:
    return shutil.which("rg")
```

每次调用都 `shutil.which("rg")` 查系统 ripgrep。`glob_files`（64–95 行）的结构是：

```
有 rg？
  ├─ 是 → 跑 rg --files -g <pattern>，returncode 0/1 算成功
  │        └─ OSError/超时/非 0,1 → 回退 _glob_walk
  └─ 否 → 直接 _glob_walk
```

几个关键决策：

- **`--hidden --no-ignore`**：rg 默认尊重 `.gitignore` 和隐藏文件规则，这里全关掉——agent 搜索要看到 `.env`、`.config/` 这类「被 git 忽略但真实存在」的文件。`--no-ignore` 的副作用是**不再自动跳过 `.git` / `node_modules`**，所以手动把 `IGNORE_GLOBS` 重新加回去（`--glob !...`）。
- **`returncode not in {0, 1}`**：rg 退出码 0 = 有命中，1 = 无命中，2+ = 出错。0 和 1 都算「正常跑完」，只有 2+ 才回退。
- **`timeout=30`**：rg 卡死（罕见，比如大仓库 + 病态 pattern）30 秒后放弃，回退纯 Python。纯 Python 也会慢，但至少不会因为 rg 的 bug 永久挂住。
- **回退是「整体回退」不是「逐条回退」**：rg 一失败，整个结果改用 `_glob_walk` 重算，不混合两套结果。

## 4. `grep_files`：搜内容（97–131 行）

```python
def grep_files(root, pattern, *, glob=None, limit=DEFAULT_LIMIT, max_line=500) -> list[str]:
```

搜**文件内容**，返回 `path:行号:内容` 的字符串列表。和 `glob_files` 同样的双实现结构，差异点：

- **`-e pattern`**：pattern 可能以 `-` 开头（搜字面 `-`），用 `-e` 显式声明它是表达式，不被当成 rg 的选项。
- **`--no-heading`**：多文件搜索时不插文件头，输出是连续的 `path:行号:内容` 行。
- **`glob` 参数**：过滤「搜哪些文件」，和「搜什么内容」（pattern）正交。同样经 `expand_braces` 展开。
- **`max_line=500`**：单行内容超过 500 字符截断成 `line[:580] + "…"`（`max_line + 80` 给 `path:行号:` 前缀留空间）。长行（压缩 JSON、长 URL）常见，不截会一条命中吃掉几 k token。
- **`limit` 条时追加 `...[truncated]`**：和 glob 一样，告诉模型「还有更多」。

## 5. `_name_matches` 与 glob 匹配（134–176 行）

纯 Python 回退版的匹配逻辑。`_name_matches`（134–145 行）：

```python
for alt in expand_braces(pattern):
    if _glob_match(rel_n, alt) or _glob_match(name_n, alt):
        return True
```

**文件名或相对路径任一命中就算匹配**——用户写 `*.py` 匹配 `src/a.py`（按文件名），写 `src/*.py` 匹配相对路径。这让回退版和 rg 的「basename 或 path 匹配」语义对齐。

`_glob_re`（152–176 行）把 gitignore/rg 风格 glob 转正则，规则：

| glob | 正则 | 语义 |
|---|---|---|
| `**/` | `(?:.*/)?` | 零或多个目录 |
| 末尾 `**` | 开头 `.*` / 否则 `(?:/.*)?` | 匹配任意深度 |
| `*` | `[^/]*` | 单星**不跨目录** |
| `?` | `[^/]` | 单个非斜杠字符 |
| 其他 | `re.escape` | 字面 |

`@lru_cache(maxsize=256)`：同一个 pattern 编译一次复用，`os.walk` 遍历大量文件时不用反复 `re.compile`。

## 6. `_glob_walk`：纯 Python 回退（179–199 行）

```python
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not (...).is_symlink()]
    for name in filenames:
        if path.is_symlink():
            continue
        if _name_matches(name, rel, pattern):
            hits.append(path)
            if len(hits) >= limit:
                return hits
```

- **`dirnames[:] = [...]` 原地剪枝**：`os.walk` 是惰性的，改 `dirnames` 列表就能阻止它进入那些目录，比事后过滤省 IO。
- **符号链接跳过**（注释点破）：链接和目标指向同一真实路径，下游 `resolve()` 后同一文件会以两个名字出现。跳过链接，保证「一个文件只出现一次」。

## 7. `_grep_walk`：纯 Python 回退（201–243 行）

```python
if root.is_file():
    files = [root]; base = root.parent
```

**root 是单个文件时只搜它**——`grep <pattern> <file>` 的语义，`base` 退到父目录让相对路径正确。

```python
try:
    text = path.read_text(encoding="utf-8")
except (UnicodeDecodeError, IsADirectoryError, PermissionError, FileNotFoundError, OSError):
    continue
```

读文件失败（二进制、无权限、竞态删除）**静默跳过**，不让一个坏文件中断整个搜索。代价：二进制文件里的匹配搜不到——rg 版能搜二进制，回退版不能，这是两套实现的**已知行为差异**。

命中行同样 `max_line` 截断、`limit` 条时 `...[truncated]`。
