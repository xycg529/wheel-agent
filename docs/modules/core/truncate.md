# `core/truncate.py` 逐段讲解

> 本篇讲工具输出的裁剪与溢写盘。上游是 [`tools/tools.md`](../tools/tools.md)（`ToolRuntime._after` 按工具声明调用），下游是 [`tools/workspace.md`](../tools/workspace.md)（溢出文件落在工作区内）。

一句话职责：**把单条工具输出裁到预算之内，完整原文写进工作区，再拼一行提示告诉模型"看到的是第几到几行、全文在哪"**。

- 行数：208 行
- 依赖：标准库（`uuid`、`dataclasses`、`datetime`、`pathlib`），无项目内 import
- 被谁用：[`tools/tools.md`](../tools/tools.md) —— 唯一调用方，`from wheel_agent.core.truncate import GREP_MAX_LINE_LENGTH, apply`（`tools/tools.py:26`）
- 相关但不同层：[`core/compact.md`](compact.md)（整段历史的压缩）、[`docs/loop-explained.md`](../../loop-explained.md)（循环怎么用工具输出）

## 目录

- [1. 预算常量与定位](#1-预算常量与定位1–14-行)
- [2. `TruncationResult`](#2-truncationresult18–31-行)
- [3. 字节与单行工具函数](#3-字节与单行工具函数34–53-行)
- [4. `truncate_head` / `truncate_tail`](#4-truncate_head--truncate_tail56–71-行)
- [5. `spill_output`](#5-spill_output74–83-行)
- [6. `with_notice`](#6-with_notice86–97-行)
- [7. `apply`](#7-apply100–123-行)
- [8. `_truncate` 核心算法](#8-_truncate-核心算法134–208-行)
- [9. 和 compact 的区别](#9-和-compact-的区别)

---

## 1. 预算常量与定位（1–14 行）

三个常量决定"多少算大"：

```python
DEFAULT_MAX_LINES = 2000      # 行预算
DEFAULT_MAX_BYTES = 50 * 1024 # 字节预算（50KB）
GREP_MAX_LINE_LENGTH = 500    # 单行字符上限
```

**双预算、先到先截**：行数管的是"模型要读多少行"，字节管的是"上下文占多少"。只按行数截会漏掉少量超长行（压缩后的 JSON、minified 代码一行就能几十 KB）；只按字节截则对普通输出过于宽松。2000 行 / 50KB 的取舍是：够放下绝大多数命令输出和文件头，又不至于一次吃掉 128K 窗口的一大块。

`GREP_MAX_LINE_LENGTH = 500` 是**字符数**不是字节数——它服务的对象是 grep 命中行，命中行通常是"文件路径:行号:内容"，500 字符足够看清内容，再长就是噪音。这个常量被 `tools/tools.py:680` 导入后传给 `grep_files(max_line=...)`，在搜索阶段就先把长行切短，形成"两层单行截断"。

模块 docstring 点明它是**行/字节级**的——和 token 无关，不涉及模型。这是刻意的：截断发生在工具层，不需要知道模型用什么分词器。

## 2. `TruncationResult`（18–31 行）

一个 dataclass，除截断后的 `text` 外，还带九项元信息：

| 字段 | 用途 |
|---|---|
| `truncated` / `truncated_by` | 是否截了、被行预算还是字节预算拦住（`"lines"` / `"bytes"`） |
| `total_lines` / `total_bytes` | 原文规模 |
| `output_lines` / `output_bytes` | 实际输出规模 |
| `start_line` / `end_line` | **1-based、含端点**的显示范围 |
| `last_line_partial` | 保留的最后一行是否是被字节预算切断的半行 |
| `spill_path` | 溢出文件相对工作区的路径 |

为什么要把元信息做进返回值：提示行要写"Showing lines 96-100 of 100"，这个数字**只有截断过程知道**——调用方拿到的只是截断后的字符串，反推不出来原文有多少行。`start_line` 用 1-based 是因为提示行和编辑器的行号约定一致，模型（和读日志的人）不用换算。

## 3. 字节与单行工具函数（34–53 行）

`utf8_len()`（34–36 行）：按字节限额时不能用 `len(str)`——中文一个字符 3 字节，`len` 会低估三倍。

`utf8_prefix()`（39–45 行）：按字节切前缀后 `decode(errors="ignore")` 丢弃不完整的尾巴。切在多字节字符中间时，那个残缺字符会被丢掉，而不是变成 `U+FFFD` 替换符——宁可少半个字符，也不要在上下文里留下乱码。

`truncate_line()`（49–53 行）：单行超长时切到 `max_chars` 再拼 `"... [truncated]"` 标记。

注意：**`truncate_line()` 在本项目里没有调用点**（`grep` 只在 `tools/rgfiles.py` 里自己做 `line[:max_line] + "…"` 截断）。它是给 `GREP_MAX_LINE_LENGTH` 配的公共 API，属于"预留给调用方的工具函数"。

## 4. `truncate_head` / `truncate_tail`（56–71 行）

两个薄包装，只差 `tail` 参数：

```python
def truncate_head(...): return _truncate(content, max_lines, max_bytes, tail=False)
def truncate_tail(...): return _truncate(content, max_lines, max_bytes, tail=True)
```

**保头还是保尾是工具语义决定的**，不是统一策略：

| 策略 | 工具 | 理由 |
|---|---|---|
| `head` | `read`、`ls`、`web_search`、`web_fetch` | 看文件/目录/搜索结果的**开头**就够了；`read` 本身还带 `offset`/`limit` 让模型翻页 |
| `tail` | `bash`、`bash_poll`、`bash_kill` | 命令结果通常在**最后**（报错、统计行）；测试输出最关键的是结尾的失败摘要 |
| `none` | `write`、`edit`、`grep`、`glob`、`plan`、`harness`、`skill` | 输出本身就是短的或模型要完整看到的 |

策略写在每个工具的 `ToolSpec.truncate` 字段上（`tools/tools.py:67`，默认 `"none"`），所以**加新工具时默认不截断**——宁可先不裁，也不要静默丢内容。

## 5. `spill_output`（74–83 行）

把完整原文写到 `<工作区>/.wheel/outputs/<时间戳>_<8位随机>.log`，返回 `Path`。

- 文件名带时间戳 + uuid，同毫秒并发也不碰撞；`.wheel/` 开头的目录在 [`tools/audit.md`](../tools/audit.md) 的 `SKIP_PARTS` 和工作区清单里被跳过，所以**溢出文件不会污染工作区指纹**——replay 对比不会因为这些日志文件而误判 `drift`。
- 落盘用 `write_text(content)`，不做任何处理：模型拿回的是**原始字节**，不是截断后再加工的版本。

溢写盘是这套设计的关键一环：截断的目的不是"丢弃信息"，而是"**不把信息塞进上下文，但保留可取回的路径**"。提示行给出相对路径，模型需要时用 `read` 加 `offset`/`limit` 分段读回。

## 6. `with_notice`（86–97 行）

把截断结果拼成模型最终看到的文本：

```python
notice = (f"[Showing lines {result.start_line}-{result.end_line} of {result.total_lines}. "
          f"Full output: {path}]{extra}")
body = result.text.rstrip("\n")
return f"{body}\n\n{notice}" if body else notice
```

- 未截断（`truncated=False`）直接返回原文，**不拼提示行**——没有提示行就是"你看到的是全部"的信号。
- `extra` 在 `last_line_partial` 时是 `" last line truncated."`，告诉模型最后一行是被字节切断的半行，别当完整行去解析。
- `body.rstrip("\n")` 后再空一行接提示行：输出末尾的换行本来就是截断拼接的副产物，去掉再统一留一个空行，视觉上提示行独立成段。
- `body` 为空时只返回提示行（否则会得到一个开头空两行的字符串）。

模型看到的实际长这样（`bash` 输出 100 行、行预算 5）：

```
exit=0
95
96
97
98
99

[Showing lines 96-100 of 100. Full output: .wheel/outputs/20260831T131239_d4ec27f6.log]
```

注意 `exit=0` 在最前面——见下一节。

## 7. `apply`（100–123 行）

工具输出截断的**总入口**：截断 → 溢写盘 → 拼提示行，一步到位。

```python
body = content
if keep_prefix and content.startswith(keep_prefix):
    body = content[len(keep_prefix):]
result = truncate_tail(body, ...) if tail else truncate_head(body, ...)
if not result.truncated:
    return content      # 没超限：原样返回，不存溢出文件
spilled = spill_output(workspace, content)
rel = _rel(workspace, spilled)
result.spill_path = rel
notice = with_notice(result, rel)
return keep_prefix + notice if keep_prefix else notice
```

**`keep_prefix` 机制**（114–117 行）：bash 输出的 `exit=` 行是模型判断成败的第一信息，绝不能被裁掉。调用方（`tools/tools.py:525–529`）先把 `exit=0\n` 剥出来，让行/字节预算**全花在真实输出上**，最后再把前缀拼回去。没有这个机制，保尾截断会把 `exit=0` 当成普通第一行一起截掉——模型只看到一堆输出，不知道命令成功了没。

**未超限不落盘**（119–120 行）：`if not result.truncated: return content`。溢出文件只在真的截断时才产生，99% 的调用不会在 `.wheel/outputs/` 里留垃圾。

**注意溢写的是 `content` 而不是 `body`**：存的是带前缀的完整原文，模型读回时看到的内容与未截断时一致。

`_rel()`（126–131 行）：把绝对路径转成工作区相对路径给提示行用；`relative_to` 失败（路径不在工作区内）时退回绝对路径，不抛异常。

## 8. `_truncate` 核心算法（134–208 行）

**先判是否超限**（139–152 行）：行数和字节数**都**在预算内才原样返回（任一超了就进入截断）。

**收集循环**（154–191 行）：按 `tail` 决定遍历方向——

```python
order = range(total_lines - 1, -1, -1) if tail else range(total_lines)
for idx in order:
    if len(kept) >= max_lines:
        truncated_by = "lines"
        break
    line = lines[idx]
    extra = utf8_len(line) + (1 if kept else 0)
    if used + extra <= max_bytes:
        ...  # 收下这行
        continue
    # 字节预算先爆
    if not kept:
        prefix = utf8_prefix(line, max_bytes)
        if prefix:
            kept.append(prefix)
            first_idx = last_idx = idx
            last_partial = prefix != line
        truncated_by = "bytes"
    else:
        truncated_by = "bytes"
    break
else:
    truncated_by = "lines" if total_lines > max_lines else "bytes"
```

几个设计点：

- **保尾用 `insert(0, line)` 逆序收集**：从后往前遍历、往列表头部插，得到的自然是从旧到新的顺序。
- **`extra` 只在 `kept` 非空时 +1**：那是拼接用的换行符。第一行不加——否则预算里会凭空多算一个不存在的换行。
- **字节预算先爆且一行未收时收半行**：`utf8_prefix` 切出不完整前缀并标 `last_line_partial=True`。取舍是"给模型一点信息"优于"给模型空字符串"——50KB 预算遇到单行 100KB 的 minified JS 时，至少能看到开头。已经收了内容时则直接停，不追加半行（半行夹在完整行中间反而误导）。
- **`for...else` 兜底**（189–190 行）：循环正常跑完（没 `break`）说明是"刚好卡在边界上"，按哪个维度超的补记 `truncated_by`。
- **返回值 `truncated=True`**（194–207 行）是硬编码的：走到这儿一定截了。

实测行为（便于对照）：

| 输入 | 参数 | 输出 | `truncated_by` |
|---|---|---|---|
| `a\nb\nc\nd\ne` | 3 行 | head: `a\nb\nc`（1–3）／tail: `c\nd\ne`（3–5） | `lines` |
| `abcde` | 3 字节 | `abc`，`last_line_partial=True` | `bytes` |
| `a\nb\nc` | 10 行 | 原样，`truncated=False` | `None` |
| `""` | — | `""`，`truncated=False`，`total_lines=1` | `None` |

最后一行值得留意：空串 `split("\n")` 得 `[""]`，行数算 1 而不是 0。

## 9. 和 compact 的区别

两者都叫"截断/压缩"，但作用在不同层，**互不替代**：

| | `core/truncate.py` | [`core/compact.md`](compact.md) |
|---|---|---|
| 作用对象 | **单条**工具输出字符串 | **整段**历史 item 列表 |
| 时机 | 工具执行后立刻（`_run` 内） | 上下文快撑满窗口时 / provider 报溢出时 |
| 判据 | 行数、字节数 | token 估算 vs `context_window` |
| 手段 | 切掉头或尾，原文落盘 | 让模型把旧前缀写成摘要 |
| 花不花钱 | 不花钱（纯字符串操作） | **花钱**（要调一次模型做摘要） |
| 信息去向 | 完整原文留在 `.wheel/outputs/` | 原文留在会话树里，摘要覆盖视图 |

一句话：**truncate 是消息内的裁剪（防一次输出炸掉上下文），compact 是消息间的压缩（防累积历史炸掉窗口）**。truncate 先发生——它让每条进上下文的工具输出都是"小且可追溯"的，compact 要总结的历史因此也小得多。

---
