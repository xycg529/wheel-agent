# `<tools/atfiles.py>` 逐段讲解

> 本篇讲 @ 文件引用补全。上游是 [ui/repl.md](../ui/repl.md)（行编辑器调用它补全和替换）与 [ui/app.md](../ui/app.md)（注入 `at_files` 回调），下游是 [tools/rgfiles.md](rgfiles.md)（文件枚举交给它）。

一句话职责：**在 REPL 输入行里定位光标处的 `@token`，并给出工作区文件候选路径**。纯函数、无状态、不触碰文件内容。

- 行数：82 行
- 依赖：
  - [`tools/rgfiles.py`](rgfiles.md) —— `glob_files()`，枚举工作区文件（优先系统 ripgrep）
  - `pathlib`（标准库）、`__future__.annotations`
- 被谁用：
  - [`ui/repl.py`](../ui/repl.md) —— `LineEditor._palette()`（469–482 行）、`complete()`（484–494 行）、Tab / 回车选中时用 `replace_at_token()`（597、631 行）
  - [`ui/app/__init__.py`](../ui/app.md) —— 367 行把 `list_at_files` 包成 `at_files=lambda tok: list_at_files(workspace, tok)` 注入编辑器

## 目录

- [1. 模块定位：补全区，不是展开区](#1-模块定位补全区不是展开区1–10-行)
- [2. `at_span()`：光标处的词边界（12–29 行）](#2-at_span光标处的词边界12–29-行)
- [3. `at_token()` 与 `replace_at_token()`（31–46 行）](#3-at_token-与-replace_at_token31–46-行)
- [4. `list_at_files()`：候选路径的排序与截断（48–82 行）](#4-list_at_files候选路径的排序与截断48–82-行)
- [5. 与 rgfiles.py 的分工](#5-与-rgfilespy-的分工)

## 1. 模块定位：补全区，不是展开区（1–10 行）

模块 docstring 把职责切得很干净：**`at_span` 定位光标处的 `@token`，`list_at_files` 给出候选路径。**

完整链路是两半：

| 半程 | 谁做 | 结果 |
|---|---|---|
| **补全**（找到路径） | 本模块 | `@src/main.py` 这段文本 |
| **展开**（读内容喂模型） | 不在本模块 | 见第 5 节 |

README（97 行）说"任务里的 `@src/main.py` 会展开成文件内容喂给模型"——**展开那一步不在这个文件里**，本模块只负责让用户输入出这个 token。模块名 `atfiles.py` 里的 "at" 指的是 `@` 符号本身。

整个模块四个函数全部是纯函数：输入字符串、输出字符串，没有类、没有全局状态、不读文件（`list_at_files` 只调 `glob_files` 拿路径列表）。这样设计让 TTY 编辑器可以每敲一个键就调一次，不必担心副作用，也天然线程安全。

导入只有一个项目内依赖：`from wheel_agent.tools.rgfiles import glob_files`。**文件枚举能力完全外包**——本模块不自己 `os.walk`。

## 2. `at_span()`：光标处的词边界（12–29 行）

```python
def at_span(buf: str, cur: int) -> tuple[int, int] | None:
    """返回光标处 @token 的 [start, end)，不是 @token 则 None。"""
```

给定整行文本 `buf` 和光标位置 `cur`，返回光标所在"词"的 `[start, end)` 半开区间；如果那个词不以 `@` 开头就返回 `None`（表示当前不该弹文件菜单）。

第一步是**光标归一化**（17–20 行）：

```python
cur = max(0, min(cur, n))   # 夹到 [0, len(buf)]
i = cur
if i > 0 and (i == n or buf[i] in " \t\n") and buf[i - 1] not in " \t\n":
    i -= 1
```

这段解决的是"光标在词尾之后"这个典型情形：用户输入 `@src/mai|` 时光标紧跟字符，但输入 `@src/main.py |`（尾部一个空格）时光标在空格上。两种都应该算作"正在编辑 `@src/main.py`"，否则用户敲完路径按一个空格，菜单就消失了。判断条件是**当前位置是空白或行尾，且前一格不是空白**——即"刚越过一个词的右边界"，此时回退一格回到词内。

然后向左右各扫一遍扩展出整词（21–25 行），分隔符只有三个：`空格`、`\t`、`\n`。注意**不看 `@` 之外的标点**，所以 `@src/main.py,` 会把逗号也算进 token 里。

最后（26–29 行）：`buf[start] == "@"` 才返回区间，否则 `None`。

**设计意图**：分隔符集合刻意做得极小，和 shell 的词切分对齐。代价是标点敏感，收益是零配置——不需要知道哪些字符在路径里合法。

## 3. `at_token()` 与 `replace_at_token()`（31–46 行）

两个薄封装，都建立在 `at_span` 之上：

- **`at_token(buf, cur)`**（31–36 行）：返回光标处 `@token` 的**完整文本**（含开头的 `@`），否则 `None`。编辑器用它拿到当前 token 去问候选列表。
- **`replace_at_token(buf, cur, replacement)`**（39–46 行）：把光标处 `@token` 整体替换掉，返回 `(新文本, 新光标位置)`。`span is None` 时原样返回。

```python
new = buf[: span[0]] + replacement + buf[span[1] :]
return new, span[0] + len(replacement)
```

**只替换 token，不动整行**——这是和斜杠命令补全的关键区别。`ui/repl.py` 的 Tab 处理（624–632 行）两条分支写得很清楚：

```python
if buf.startswith("/") and "\n" not in buf:
    buf = pick          # 命令：整行替换
    cur = len(buf)
else:
    buf, cur = replace_at_token(buf, cur, pick)   # @token：只替换 token
```

命令以 `/` 开头、占满整行，所以整行替换；`@` 引用嵌在任务句子中间（"看一下 @src/main.py 里的 bug"），只能替换那一段，光标落到替换文本的末尾，用户接着往下打字。

## 4. `list_at_files()`：候选路径的排序与截断（48–82 行）

真正的补全逻辑。签名 `list_at_files(root, token, limit=12)`，返回形如 `["@src/main.py", "@src/util.py"]` 的**带 `@` 前缀**的相对路径列表（82 行：`return ["@" + rel for _rank, rel in hits[:limit]]`）。

返回时重新加上 `@`，是因为调用方拿它直接做行内替换（`replace_at_token` 的参数是完整替换文本），省得每个调用点自己拼。

### 4.1 token 归一化（50–53 行）

```python
prefix = token[1:] if token.startswith("@") else token
prefix = prefix.replace("\\", "/").lower()
if prefix.startswith("./"):
    prefix = prefix[2:]
```

三件事：剥掉开头的 `@`（token 一定带 `@`，但函数也容忍不带的情况）；反斜杠统一成正斜杠（Windows 输入）；**整体转小写做大小写不敏感匹配**。剥 `./` 前缀是因为用户常按 shell 习惯写 `@./src/main.py`，而候选路径是 `src/main.py`——不剥就一个都匹配不上。

### 4.2 枚举与去重（55–66 行）

```python
root_real = root.resolve()
for path in glob_files(root, "*", limit=200):
    real = path.resolve()
    rel = real.relative_to(root_real).as_posix()
```

- 模式用 `"*"`，即**枚举工作区全部文件**（受 `rgfiles` 自己的 `SKIP_DIRS` 排除规则约束：`.git`、`.venv`、`node_modules`、`build`、`dist` 等都不进来）。
- `limit=200` 是枚举上限（rgfiles 的 `DEFAULT_LIMIT` 也是 200）。补全不需要全库扫描，200 个够填满任何一屏菜单，同时**给大仓库设了硬天花板**。
- 先 `resolve()` 再 `relative_to()`：把工作区可能存在的符号链接解析成真实路径，避免 `rel` 里出现 `..`。
- `except (OSError, RuntimeError, ValueError): continue`——`relative_to` 在路径不在 root 下时抛 `ValueError`，`resolve` 在链接循环时抛 `RuntimeError`，逐条跳过而不是整批失败。
- `seen` 集合按 `str(real)` 去重（62–64 行）：`rgfiles` 已经跳过符号链接，但 `--hidden` 等路径下仍可能同一真实文件出现两次，去重保证菜单里不出现重复项。

### 4.3 两级排序（67–79 行）

```python
low = rel.lower()
name = Path(rel).name.lower()
if prefix == "":
    rank = 0
elif name.startswith(prefix) or low.startswith(prefix):
    rank = 0
elif prefix in low:
    rank = 1
else:
    continue
hits.append((rank, rel))
```

`rank` 是**两级优先级**：

- **rank 0（前缀命中）**：文件名以 prefix 开头（`name.startswith`），**或**完整相对路径以 prefix 开头。输入 `@core/lo` 能命中 `core/loop.py`（路径前缀），输入 `@lo` 能命中 `core/loop.py`（文件名前缀）——两种直觉都照顾到。
- **rank 1（子串命中）**：prefix 出现在路径任意位置。输入 `@loop` 命中 `core/loop.py`。优先级更低，因为子串命中通常更"发散"。
- 都不命中就 `continue` 丢弃。

`prefix == ""` 特殊处理成 rank 0：用户刚敲下 `@` 还没输入任何字符时，**所有文件都是 rank 0**，即菜单按路径字典序展示工作区文件（空 prefix 时 `startswith("")` 恒真，这里写分支只是少算几次字符串操作）。

最后 `hits.sort()`（80 行）对 `(rank, rel)` 元组排序：先按 rank 分组，同 rank 内按路径字典序稳定排列——**路径字典序意味着同目录的文件会挨在一起**，视觉上成组，比按匹配分数排序更好扫。

### 4.4 两道截断（69–71、82 行）

```python
hits.append((rank, rel))
if len(hits) >= 80:
    break
...
return ["@" + rel for _rank, rel in hits[:limit]]
```

- **收集上限 80**（作者设定）：即使 200 个候选全部匹配，也只收集 80 个就停止枚举。因为最终只展示 `limit=12` 个，**收集更多是浪费**（一次 Tab 按键的延迟预算里不该跑满全库）。80 是 12 的约 7 倍，留出余量让排序后的前 12 名足够有代表性。
- **返回上限 `limit=12`**（默认）：菜单高度。12 行在任何终端里都不需要滚动，和斜杠命令菜单的观感一致。

注意排序发生在截断**之前**（80 行 sort，82 行切片），所以展示的是全局最优的 12 个，不是"先枚举到的 80 个里的前 12"。

## 5. 与 rgfiles.py 的分工

两者是**枚举 vs. 交互**的上下游关系：

| | [`tools/rgfiles.py`](rgfiles.md) | `tools/atfiles.py`（本篇） |
|---|---|---|
| 定位 | 文件搜索基础设施 | REPL 交互层 |
| 调用者 | `tools.py` 的 `glob` / `grep` **工具**（给模型用）、本模块 | `ui/repl.py`（给人用） |
| 接口 | `glob_files(root, pattern, limit)` / `grep_files(...)` | `at_span` / `at_token` / `replace_at_token` / `list_at_files` |
| 关心 | 怎么**快**地找出匹配文件（rg 优先、Python 回退、gitignore 风格 glob） | 光标在哪个词、候选怎么**排序展示** |
| 输入 | glob 模式（`"*.py"`、`"src/**/*.md"`） | `@token` 文本（`@src/mai`） |
| 输出 | `list[Path]` 绝对路径 | `list[str]` 带 `@` 的相对路径 |

关键分工点：**本模块不做 glob 模式匹配**。它用 `glob_files(root, "*")` 拿回全部文件，然后自己做 `startswith` / `in` 前缀与子串判断。

为什么不让 rgfiles 代劳（比如传 `@token` 转成的 `"mai*"`）？

1. 排序需求不同：本模块要区分"文件名前缀命中"和"路径子串命中"两种优先级，rgfiles 的 glob 语义表达不了这个分级。
2. 转义问题：`@` token 是自由文本，可能含 glob 元字符（`[`、`*`、`?`），转 glob 要先转义，反而更绕。
3. 结果量级不同：工具调用可以接受 200 条结果进模型上下文，补全菜单只要 12 条，且要立即响应。

代价是补全在大仓库上每次按键都要枚举 200 个文件。这是有意接受的（枚举有 `SKIP_DIRS` 剪枝 + rg 加速），换来的是零转义、零模式语法的心智负担。

### 展开那一半在哪

补全只是前一半。用户提交含 `@src/main.py` 的任务后：

- [`ui/app/__init__.py`](../ui/app.md) 的 `start_task()`（492–493 行）对输入做 `expand_skill_command()`（展开 `/skill:`，**不处理 `@`**），然后交给 `run_task()`；
- 含 `@` 引用的任务文本**原样**送进 [loop.py](../../loop-explained.md) 当 user 消息；
- 真正把文件内容取出来的是模型自己调 `read` 工具（[`tools/tools.py`](tools.md) 的 `_read()`，652 行）——系统提示里写了 `Tools: read, ls, glob, grep, ...`，且 `format_skills_xml` 明确提示"Workspace skills can also be read"。

也就是说：**`@` 补全是为了让人类少打字、少打错路径，展开成内容是模型读文件的能力，不是文本替换。** README 说的"展开成文件内容喂给模型"指的是这个模型侧行为。
