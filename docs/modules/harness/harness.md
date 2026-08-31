# `harness/harness.py` 逐段讲解

> 本篇讲 harness（持续学习）的存储与变更层。上游是 `harness` 工具（[tools/tools.md](../tools/tools.md)）、`/refine` 流程（[harness/refine.md](refine.md)）和主循环（[loop-explained.md](../../loop-explained.md) 第 3、10 节），下游只依赖标准库。

一个跨轮次、跨会话的 prompt 笔记与事实记忆存储：两层作用域（local/global）+ 两种内容类型（prompt/memory）+ 带 CAS 校验和回滚的变更历史。

- 行数：624 行
- 依赖：只有标准库（`json` / `os` / `re` / `threading` / `itertools` / `dataclasses` / `datetime` / `pathlib` / `secrets`）——它刻意不依赖 `core/` 下任何模块，所以能被 UI 层（[ui/app-refine.md](../ui/app-refine.md)）和工具层（[tools/tools.md](../tools/tools.md)）同时复用而不产生循环导入。
- 被谁用：
  - [tools/tools.md](../tools/tools.md)：`harness` 工具把模型参数转交给 `HarnessStore.dispatch()`。
  - [harness/refine.md](refine.md)：`/refine` 用 `snapshot_state` / `apply_proposal` / `rollback_proposal` / `load_history` 跑完整的"规划 → 应用 → 记历史"流程。
  - [core/prompt.md](../core/prompt.md)：`system_prompt()` 调 `format_harness_for_prompt(state)` 把笔记拼进系统提示。
  - [docs/loop-explained.md](../../loop-explained.md) 第 10 节：`store.dirty` 触发系统提示重拼 + 缓存纪元自增。
  - [ui/app-refine.md](../ui/app-refine.md)：`HarnessStore.for_workspace(..., session_path=session.path)` 绑定会话级存储。

## 目录

- [0. 函数/类速查表](#0-函数类速查表)
- [1. 模块常量与两个工具函数（1–54 行）](#1-模块常量与两个工具函数-154-行)
- [2. 数据模型：`HarnessEntry` 与 `HarnessState`（56–95 行）](#2-数据模型harnessentry-与-harnessstate-5695-行)
- [3. 路径推导：global / local / history（92–119 行）](#3-路径推导global--local--history-92119-行)
- [4. `load_state`：损坏一律降级（121–147 行）](#4-load_state损坏一律降级-121147-行)
- [5. `save_state`：写 tmp 再原子替换（149–179 行）](#5-save_state写-tmp-再原子替换-149179-行)
- [6. `snapshot_state`（181–193 行）](#6-snapshot_state-181193-行)
- [7. `merge_states`：两层作用域的合并规则（195–217 行）](#7-merge_states两层作用域的合并规则-195217-行)
- [8. `format_harness_for_prompt`：渲染进系统提示（219–260 行）](#8-format_harness_for_prompt渲染进系统提示-219260-行)
- [9. `apply_proposal`：带 CAS 的批量应用（262–373 行）](#9-apply_proposal带-cas-的批量应用-262373-行)
- [10. `rollback_proposal`：用 before 快照回滚（375–411 行）](#10-rollback_proposal用-before-快照回滚-375411-行)
- [11. 历史日志：fsync 追加与容错读取（413–441 行）](#11-历史日志fsync-追加与容错读取-413441-行)
- [12. `HarnessStore`：工作区门面（443–545 行）](#12-harnessstore工作区门面-443545-行)
- [13. 私有辅助函数（547–624 行）](#13-私有辅助函数-547624-行)

---

## 0. 函数/类速查表

| 名字 | 行号 | 职责 |
|---|---|---|
| `KINDS` / `SCOPES` / 常量 | 19–25 | 两种内容类型、两层作用域、文件名与容量上限 |
| `now` / `slug` | 28–36 | 本地 ISO 时间戳 / 文本压成 ID slug |
| `generate_refinement_id` | 44–53 | 生成全局唯一 refine ID（进程 nonce + 计数器） |
| `HarnessEntry` | 56–71 | 一条笔记的 dataclass |
| `HarnessState` | 73–90 | 一个存储的状态：按 kind 分桶的 entries + refinements |
| `empty_state` | 92–94 | 新建空状态 |
| `global_harness_dir` | 97–100 | `~/.wheel/harness/` |
| `local_harness_path` | 103–112 | 会话级状态文件路径（会话日志旁）或工作区 `.wheel/` 兜底 |
| `history_path` | 114–119 | 从状态文件路径推导历史文件路径 |
| `load_state` | 121–147 | 读 JSON；任何损坏降级为空状态，不抛异常 |
| `save_state` | 149–179 | 写 tmp + 原子 replace |
| `snapshot_state` | 181–193 | 全量深拷贝（CAS baseline 用） |
| `merge_states` | 195–217 | global + local 合并成提示用视图 |
| `format_harness_for_prompt` | 219–260 | 渲染成系统提示文本块（限量截断） |
| `apply_proposal` | 262–373 | 应用一份提案（edits 列表），带乐观并发检查 |
| `rollback_proposal` | 375–411 | 从历史记录构造回滚提案 |
| `append_history` / `load_history` | 413–441 | 历史日志的 fsync 追加与容错读取 |
| `HarnessStore` | 443–545 | 工作区门面：加载/合并/写目标/记历史/工具入口 |
| `_parse_entry` | 547–575 | 从 JSON 解析单条 entry，字段不合预期返回 None |
| `_validate_edit` | 577–596 | 校验单条 edit；保护 base system prompt 不可写 |
| `_compact` / `_as_bool` / `_format_tool_result` | 598–624 | 单行截断 / 宽容转 bool / 结果格式化 |

---

## 1. 模块常量与两个工具函数（1–54 行）

```python
KINDS = ("prompt", "memory")
SCOPES = ("local", "global")
GLOBAL_DIRNAME = "harness"
STATE_NAME = "harness_state.json"
HISTORY_NAME = "refinements.jsonl"
MAX_ENTRIES_PER_KIND = 8   # 系统提示里每类最多展示多少条
MAX_CONTENT = 240          # 单条内容在提示里的展示上限（字符）
```

`KINDS` 是内容的**语义分类**，`SCOPES` 是存储的**生命周期分类**，两者正交（local 的 prompt、global 的 memory 都合法）：

- **`prompt` = 行为策略**（`HarnessEntry` docstring 原话："prompt 是行为策略"）。回答"agent 应该怎么做事"：踩过的坑、用户纠正过的做法、项目约定。它会被拼进系统提示并**指令模型遵守**（见第 8 节的 "Follow them."）。
- **`memory` = 事实记忆**。回答"这个世界是怎样的"：项目事实、决策记录、用户偏好、失败过的方案。它不直接下指令，但影响模型判断。

两者的**写入时机**也在工具描述里被明确约束（[tools/tools.md](../tools/tools.md)）："kind=prompt is a behavioral policy; kind=memory is a fact/preference/decision"、"Do not store one-off task progress"——harness 只存耐久经验，一次性任务进度留在上下文里。

`MAX_ENTRIES_PER_KIND = 8` 和 `MAX_CONTENT = 240`：这两个数直接决定 harness 块塞进系统提示的体积上限，`8 × 2 类 × (240 + 约 40 字符的元信息) ≈ 4.5 KB`。系统提示是**前缀缓存**的一部分（见 [docs/loop-explained.md](../../loop-explained.md) 第 10 节），harness 块持续增长会把每轮都要重算的前缀越推越大，也会挤占上下文窗口。8 条是"够放近期高频教训"和"不淹没基础提示"之间的折中，240 字符约等于一条推文——足够写清一条教训，长到写论文就该用 `path` 拆分或压缩措辞了。超出的条目不是丢弃，而是折叠成一行 `+N more {kind} entries`（第 8 节）：**存储不设上限，只有展示设上限**——这是刻意的选择，避免"写满了就丢知识"。

`now()`（28–30 行）用本地时区 ISO 时间戳，贯穿全模块的时间字段。`slug()`（33–36 行）把任意文本压成 ID slug（小写字母数字下划线，非法字符转 `_`，空则回落 fallback，截到 80 字符）——create 时没给 id 就用它从 title 生成稳定 id，保证同一条笔记反复 update 落在同一个 key 上。

**ID 生成是这个文件里唯一用到 `threading` 的地方**（39–53 行）：

```python
_id_counter = itertools.count(1)
_id_lock = threading.Lock()
_PROC_NONCE = token_hex(2)      # 4 个 hex 字符
```

`generate_refinement_id()` 拼 `refine_<17位时间戳>_<4位nonce><4位计数器>`。为什么时间戳不够：`refine` 可能由后台线程自动触发（[ui/app-refine.md](../ui/app-refine.md) 的 `/refine auto`），同一批次里两次 harness 调用会落在同一毫秒；而历史文件在 local/global 两个作用域间是**合并读取**的（见 [harness/refine.md](refine.md) 的 `run_refine`），ID 撞车会让回滚找错目标。进程 nonce 隔开不同进程，计数器隔开同进程内的并发调用，两者相加在共享历史文件下也不可能碰撞。`_id_counter` 是 `itertools.count`，本身自增非原子，所以外面套 `_id_lock`。

注意这里**只有 ID 生成是线程安全的**；状态读写没有全局锁，保护它们的机制是 CAS 和原子替换（第 5、9 节）。

---

## 2. 数据模型：`HarnessEntry` 与 `HarnessState`（56–95 行）

`HarnessEntry`（56–71 行）一条笔记的字段，按用途分三组：

| 字段 | 用途 |
|---|---|
| `id` / `kind` / `title` / `content` | 主体内容 |
| `path` / `scope` / `metadata` | 组织维度：`path` 是分组路径（默认 `"general"`，在提示里参与排序）、`scope` 标注归属、未使用的扩展位 |
| `source` / `created_at` / `updated_at` / `version` | 溯源与并发：`source` 区分 `"agent"`（工具直写）与 `"refine"`（批量提炼，第 9 节）、`version` 每次 update +1 |

`version` 不是乐观锁——真正的并发检查走 `baseline` 全量快照比对（第 9 节），`version` 只用于展示（`v{entry.version}`）和人工判断改过几次。

`HarnessState`（73–90 行）：

```python
entries: dict[str, dict[str, HarnessEntry]]   # {kind: {entry_id: entry}}
refinements: list[dict[str, Any]]             # 内嵌的历史（渲染用）
path / scope / schema                          # 落盘位置、作用域、schema 版本
```

`entries` 是**按 kind 分桶的两级字典**，不是扁平列表——因为渲染、合并、校验全都按 kind 分组进行，分桶省掉每次 filter。`refinements` 是状态的**内嵌副本**，真正的历史在独立的 `refinements.jsonl`（第 11 节）；两份并存是为了让 `format_harness_for_prompt` 在一次调用里同时拿到笔记和最近改动，不必再读磁盘。

`clone_entry()`（81–89 行）用 `HarnessEntry(**asdict(entry))` 深拷贝单条——`asdict` 会递归展开嵌套的 `metadata`，所以拷出来的是真副本，不是共享引用。CAS 比对需要"规划时刻的原值"，若直接持有引用，规划期间的修改会污染 baseline，检查就永远通过。

---

## 3. 路径推导：global / local / history（92–119 行）

```python
def global_harness_dir(home=None) -> Path:      # ~/.wheel/harness/
def local_harness_path(workspace, session_path=None) -> Path
def history_path(state_path) -> Path
```

**global**（97–100 行）：`~/.wheel/harness/harness_state.json`。跨会话、跨工作区共享。

**local**（103–112 行）分两种情况：

- 有会话：`Path(session_path).with_suffix(".harness.json")`——**就在会话日志旁边**（`session.jsonl` → `session.harness.json`）。设计意图：会话级笔记跟随会话，会话文件被删/被归档，笔记一起走；`/tree` 分叉出的新会话天然带着分叉时刻的笔记副本思路。
- 无会话：退回工作区 `.wheel/harness_state.json`（一次性 `wheel "任务"` 模式，见 [ui/app.md](../ui/app.md)）。

**history_path**（114–119 行）从状态路径推导历史路径，两边后缀可互推：`.harness.json` → `.refinements.jsonl`（`with_suffix("").with_suffix(...)` 剥两次后缀），否则同级放 `refinements.jsonl`。

这套"路径可互相推导"的约定意味着：只要拿到 session 路径，local 状态和它的历史都能算出来，不需要额外的注册表文件。

---

## 4. `load_state`：损坏一律降级（121–147 行）

读取策略是**任何异常都不上抛**：

- 文件不存在 → 空状态；
- JSON 解析失败 / 读失败（`OSError, ValueError`）→ 空状态；
- 顶层不是 dict → 空状态；
- `schema` 值坏了 → 静默降级到 `1`（`try/except: pass`，注释写明"和其他损坏字段一样降级到 v1"）；
- 单条 entry 字段不合预期 → `_parse_entry` 返回 `None`，跳过这一条，**其余条目照常加载**。

设计取舍写在 README 的 harness 小节里："harness 状态文件损坏时降级为空状态，应用不崩"。harness 是**增强件**不是关键路径——笔记读不出来，agent 只是少了些经验，不应该因此无法启动。代价是静默：一份被写坏的文件会在毫无提示的情况下变成空状态。缓解手段在第 5 节——不让它写坏。

`_parse_entry`（547–575 行，第 13 节）的宽松策略值得一提：`title` 和 `content` 必须是 `str`（笔记的硬要求，缺了没法渲染），其余字段全有兜底（`version` 转 int 失败回落 1、`scope` 不在 `SCOPES` 里就用传入的默认 scope、时间缺失填 `now()`）。

---

## 5. `save_state`：写 tmp 再原子替换（149–179 行）

```python
tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
try:
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
finally:
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
```

docstring 把理由讲得很清楚：**"写到一半崩了只会留下旧文件或新文件，不会留下半个 JSON。（`load_state` 虽然能降级，但半文件会丢光所有条目而不只是解析失败。）"**

这是和第 4 节配套的防御：降级读取的代价是"半文件 = 全丢"，所以写入侧必须保证**不会留下半文件**。`Path.replace()` 在同一文件系统上是原子 rename，要么看到旧内容、要么看到新内容。tmp 文件名带 pid，多个进程同时写不会抢同一个 tmp。`finally` 里清理 tmp 并吞掉 `OSError`（已被 replace 掉的 tmp 不存在了，重复清理失败无害）。

---

## 6. `snapshot_state`（181–193 行）

全量深拷贝（逐条 `HarnessEntry(**asdict(entry))`，`refinements` 用 `list()` 浅拷列表本身——里面的 dict 是只读消费，不复制）。

唯一用途：给 `apply_proposal` 提供 `baseline`。调用方是 [harness/refine.md](refine.md) 的 `run_refine`，它"规划前的快照，应用时做并发检查"。

---

## 7. `merge_states`：两层作用域的合并规则（195–217 行）

```python
def merge_states(global_state, local_state) -> HarnessState:
```

合并顺序是 **global 先、local 后**，两者都深拷进新状态，并**强制覆盖 `scope` 字段**（`cloned.scope = "global"` / `"local"`）——文件里记的 scope 不可信，以"从哪个状态里读出来的"为准。

**同 id 冲突的处理**（212–215 行）是本段最关键的一处：

```python
key = f"local:{eid}" if eid in merged.entries[kind] else eid
merged.entries[kind][key] = cloned
```

local 与 global 存在同 id 条目时，local 那份用 `local:` 前缀 key 存进去，**两边都保留、都不覆盖**。为什么不用"local 覆盖 global"（常规配置合并的直觉）："During a local refinement, treat global entries as read-only context. Create a local override instead of updating them."（[harness/refine.md](refine.md) 的 `REFINEMENT_INSTRUCTIONS`）——global 是全项目共享的，一次会话级 refine 不该悄悄改掉它。两边都进提示，模型自己看到两份并判断用哪份。

代价：合并视图里的 key 和存储里的 id 不一致（多了 `local:` 前缀）。`apply_proposal` 走的是 `target()` 拿到的**未合并的目标状态**，用的是原始 id，所以这条前缀不会污染写入路径。

`schema` 取两边最大值——schema 是格式版本，取高水位保证读侧按新格式解析。

`refinements` 也是 global 在前、local 在后拼接（`list(global_state.refinements)` 再 extend local 的），历史合并同理。

---

## 8. `format_harness_for_prompt`：渲染进系统提示（219–260 行）

输出给 [core/prompt.md](../core/prompt.md) 的 `system_prompt()` 拼接。开头三行是定调：

```
# Continual harness
Prompt notes and memories persist outside the chat. Follow them. The base system prompt is immutable.
```

两句话各司其职：**"Follow them"** 把笔记从"参考资料"升格为"必须遵守的指令"——这就是为什么 `kind=prompt` 的笔记是行为策略；**"The base system prompt is immutable"** 划出边界，防止模型以为能用 harness 改掉基础提示（代码侧由 `_validate_edit` 强制，第 13 节）。

空状态时的引导语也有意图：`"No saved entries. Use the harness tool for durable lessons; skip one-off task state."` ——告诉模型这个工具该在什么时机用。

**排序是稳定排序**（235–238 行）：

```python
key=lambda item: (item.path, item.title, item.id)
```

注释写明理由："稳定排序：提示前缀可复用缓存"。笔记条目的增删会让插入顺序变化，若按插入顺序渲染，系统提示这个**前缀**就会每次抖动，前缀缓存全部失效。按内容排序后，只要笔记内容不变，渲染出的文本块逐字节稳定——缓存就能跨轮命中（缓存机制见 [core/model.md](../core/model.md)）。

**限量与折叠**（240–251 行）：每类取前 `max_entries` 条；`overflow = len(entries) - min(len(entries), max_entries)`，有溢出就追加 `- +N more {kind} entries`。注意**截断发生在排序之后**：`entries[:max_entries]` 是排序结果的前 8 条，所以"哪 8 条能进提示"由 `path` + `title` 字典序决定，而非时间顺序——新写的笔记未必进得去提示。这是 `path` 字段存在的实际价值：想让某条笔记稳定出现在提示里，给它一个字典序靠前的 `path`。

**历史部分**（253–259 行）只取**最近 5 条** refinements，展示 `trigger`（截断到 `max_content`）和前 6 个 changes。"让模型知道刚改过什么"——模型能看到上一条笔记刚被 refine 改过，避免重复提出同一条。

`max_content=None` 时不截断（`_compact` 被跳过），给测试和 `/harness` 全量展示留口子。

---

## 9. `apply_proposal`：带 CAS 的批量应用（262–373 行）

整个模块的核心函数。输入一份提案（`{summary, rationale, expectedOutcome, edits: [...]}`），输出完整结果记录。

### 逐条 edit 的处理（275–368 行）

**id 计算**（281–286 行）：create 且没给 id 时用 `slug(title or kind, ...)` 生成；update/delete 必须用现成 id（`computed_id` 为 `""`）。

**校验**（288–291 行）：`_validate_edit` 不通过就把这条 edit 记成 `applied: False` + `error`，**继续处理后面的 edit**——单条非法不该让整批作废。

**乐观并发检查**（293–307 行）是重点：

```python
if baseline is not None and key not in seen:
    expected = baseline.clone_entry(kind, edit_id)
    current = asdict(before) if before else None
    wanted = asdict(expected) if expected else None
    if json.dumps(current, sort_keys=True) != json.dumps(wanted, sort_keys=True):
        applied.append({..., "before": current, "applied": False,
                        "error": "entry changed during refinement planning"})
        continue
```

机制是 **compare-and-swap，比较对象是整条 entry 的规范化 JSON**（`json.dumps(..., sort_keys=True)` 保证键序一致，避免字典顺序差异造成误判）。规划时拍的 baseline 与"当前值"逐字节比对，不一致就拒掉这条 edit。

两个细节：

- **`key not in seen`**：同一条 entry 在本批次里已经被改过一次（`seen` 记录已处理的 `kind:id`），就不再拿 baseline 比——因为当前值已经是本批次自己改的结果，必然与 baseline 不符。**批次内的连续修改（先 create 再 update）是合法的**，CAS 只拦"批次外的并发修改"。
- **不是版本号检查**：没有 `version` 字段比对，用的是**全量内容**比对。好处是能检出"版本没变但内容被改"的情况（比如手工编辑文件后没改 version）；代价是内容大时比对开销高——对 8 条量级的笔记可以忽略。

**动作分支**（309–368 行）：`delete` / `create`（已存在则报错）/ `update`（不存在则报错）各自的边界检查，然后构造新 `HarnessEntry`：

```python
scope=before.scope if before else target_scope,
source="refine",                       # 标记来源：agent 直接写 vs refine 批量改
created_at=before.created_at if before else now(),
updated_at=now(),
version=(before.version + 1) if before else 1,
```

注意 `scope` 沿用旧值——update 不会把条目挪到别的作用域；`created_at` 保留，只有 `updated_at` 刷新。`source="refine"` 与 `HarnessEntry` 默认的 `"agent"` 区分开：**工具直写标 agent，批量提炼标 refine**，`/refine` 的历史记录里能看出这条笔记是人（或模型单次调用）写的，还是复盘流程批量改的。

**历史与落盘**（369–373 行）：这次 refine **本身也进历史**（`state.refinements.append({id, trigger, changes, evidence, outcome, created_at})`），然后 `save_state(state)`。注释："回滚就读它"——历史记录里带着每条 edit 的 `before`/`after` 快照，这是回滚能精确还原的唯一依据（第 10 节）。

`changes` 列表只汇总 `applied` 为真的项（`f"{action} {kind}:{id}"`），失败的 edit 不进 changes——历史是"这次实际改了什么"的记录。

返回值是一份完整结果：id、summary、rationale、expectedOutcome、**appliedEdits**（逐条的 applied/before/after/error）、harnessStatePath、rollbackOf、scope。这份 dict 既回给模型（格式化后），也整条写进历史文件（第 11 节）。

---

## 10. `rollback_proposal`：用 before 快照回滚（375–411 行）

从一次 refine 的**结果记录**构造回滚提案，逐条反向回放：

```python
for edit in reversed(target.get("appliedEdits") or []):
    if not edit.get("applied"): continue
    before, after = edit.get("before"), edit.get("after")
    if before:      # 改之前有值 → 还原成 before
        edits.append({"action": "update" if after else "create", ...})
    elif after:     # 改之前没值（是 create）→ 删掉
        edits.append({"action": "delete", ...})
```

三个设计点：

1. **`reversed()` 逆序**：多次 refine 可能改同一条 entry，正序回滚会把中间状态覆盖回去，只有逆序才能回到最初。
2. **`after` 的有无决定动作是 update 还是 create**：`before` 存在说明这条 entry 原本就有——若 `after` 也存在，原本是 update，回滚就是再 update 回 before；若 `after` 不存在（不可能同时出现，但逻辑上留了口子）当作 create 还原。
3. **`before` 为空 + `after` 存在 = 原操作是 create**，回滚就是 delete。

跳过 `applied: False` 的 edit——它们没动过状态，无需还原。

生成的提案走**同一条 `apply_proposal` 路径**应用，但 [harness/refine.md](refine.md) 传的是 `baseline=None`："回滚不做并发检查（它是恢复）"。理由：回滚的目标就是强行动作，若因为期间又被改过而整批被拒，就永远恢复不了。

---

## 11. 历史日志：fsync 追加与容错读取（413–441 行）

`append_history`（413–423 行）：

```python
with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(result, ensure_ascii=False) + "\n")
    fh.flush()
    os.fsync(fh.fileno())
```

**每行 fsync**。docstring 说明取舍："历史是回滚要读回的日志；每行 fsync 后崩溃最多丢正在写的那条，不会连累之前的。" fsync 是笔可观的开销（一次磁盘同步），但对"崩溃后必须能回滚"的日志来说值——refine 不是高频操作（默认每 8 个用户回合自动一次）。

`load_history`（425–441 行）跳过空行、跳过 JSON 解析失败的行，且**只收带 `id` 和 `appliedEdits` 的完整记录**（`if isinstance(item, dict) and item.get("id") and "appliedEdits" in item`）。后一个过滤条件是刚性的：没有 `appliedEdits` 的记录无法构造回滚提案（第 10 节全靠它），收进来只会让 `/refine rollback <id>` 报出找不到的错误。

历史文件是**只追加**的：回滚不删除原记录，而是追加一条 `rollbackOf` 指向它的新记录（第 9 节的 `rollback_of` 参数）。所以历史是完整的变更审计链，能看到"改了 → 回滚了 → 又改了"的全过程。

---

## 12. `HarnessStore`：工作区门面（443–545 行）

把上述所有函数包成一个对象，供调用方使用。

**`for_workspace()`**（458–474 行）：同时加载两个状态——

```python
local_path = local_harness_path(workspace, session_path)
global_path = global_harness_dir(home) / STATE_NAME
return cls(load_state(local_path, "local"), load_state(global_path, "global"), interactive=interactive)
```

主循环在 [docs/loop-explained.md](../../loop-explained.md) 第 3 节里就是这么构造的，传 `session_path=session.path` 把 local 绑到会话文件旁。

**`merged()`**（476–478 行）：`return merge_states(self.global_state, self.local)`。**每次调用都重新合并**（深拷全部条目），不是缓存的——因为系统提示需要在笔记变更后立刻拿到新视图。调用方是 `system_prompt(..., harness=store.merged())`（[core/prompt.md](../core/prompt.md)）。

**`target(global_)`**（480–484 行）：选写目标。这里有一条**安全护栏**：

```python
if global_ and not self.interactive:
    raise ValueError("global harness writes are interactive-only")
```

非交互模式（`wheel --json "任务"`、无人值守）**禁止写 global**。理由：global 是跨会话共享的，无人值守的一次性任务没有用户监督，若悄悄写进全局笔记，会污染之后所有会话的系统提示。local 写入不受限——它只影响当前会话/工作区。

**`history_file(global_)`**（486–491 行）：无路径状态时**显式 `raise ValueError` 而非 assert**（注释写明理由）。assert 会被 `-O` 优化掉，而这里是公共 API 的错误处理。

**`record(result)`**（493–498 行）：

```python
global_ = result.get("scope") == "global"
path = self.history_file(global_)
append_history(path, result)
self.dirty = True
```

**`dirty = True` 是这个文件与主循环唯一的耦合点**。作用链条：[docs/loop-explained.md](../../loop-explained.md) 第 10 节——主循环在每轮工具执行后检查 `store.dirty`，为真就重拼系统提示（`system_prompt(..., harness=store.merged())`）并让 `session.cache_epoch += 1`，从而让旧的 prompt cache key 失效、新前缀从下一轮开始重建缓存。**不重拼**的话，模型要等到下一轮（甚至下个会话）才看到刚写进去的笔记；**不换缓存纪元**的话，会命中带着旧笔记的缓存，笔记"写了但没生效"。

**`dispatch(args)`**（500–545 行）是 `harness` 工具的入口（[tools/tools.md](../tools/tools.md) 的 `_harness` 直接转发），四个动作：

- `list` → 返回 `format_harness_for_prompt(self.merged())`（和进系统提示的是同一段文本，模型看到的就是它自己将来会看到的）；
- `create` / `update` / `delete` → **统一包装成单条 edit 的提案**，走 `apply_proposal`。

这个"单条 edit 的提案"设计是刻意的：工具直写与 `/refine` 批量提炼共用同一套校验、CAS、历史记录逻辑，没有两份实现。工具路径传 `baseline=None`（不做并发检查）——工具调用是同步的、单次的，规划和应用的间隔为零，没有并发窗口。

两个细节：

```python
edit["id"] = entry_id or None if action == "create" else entry_id
```

create 且没给 id 时传 `None`，让 `apply_proposal` 用 `slug(title)` 生成（第 9 节）。

```python
if not applied:
    error = failed[0]["error"] if failed else "no edits applied"
    raise ValueError(error)   # 让循环把它当工具错误回给模型
```

失败时**抛 `ValueError`**——工具层的约定是把 `ValueError` 转成 `ToolResult(is_error=True)`，模型于是收到一条错误结果，能自己纠正参数重试（比如改成 update 一个已存在的 id）。成功则 `self.record(result)`（记历史 + 标 dirty）并返回格式化文本。

---

## 13. 私有辅助函数（547–624 行）

**`_parse_entry`**（547–575 行）见第 4 节。

**`_validate_edit`**（577–596 行）校验单条 edit，返回错误消息或 None。三条规则，其中一条是安全边界：

```python
if kind == "prompt" and (edit.get("id") == "base_system_prompt" or computed_id == "base_system_prompt"):
    return "base system prompt is not editable"
```

**base system prompt 不可被 harness 修改**。这条检查同时比对原始 id 和 slug 生成的 id（防止用 `"Base System Prompt"` 这种能 slug 成 `base_system_prompt` 的 title 绕过）。配合第 8 节提示里的 "The base system prompt is immutable"：提示侧声明、代码侧拦截，双保险。若笔记能改写基础提示，一次模型自发操作就能改变 agent 的根本行为约束（包括"只能在工作区内操作"这类）。

其余两条：action 必须在 `{create, update, delete}`、kind 必须在 `KINDS`、非 create 必须有 id、非 delete 必须有 title 和 content。

**`_compact`**（598–604 行）：`re.sub(r"\s+", " ", text)` 压成单行再截断，超长时留 `max_length - 3` 字符 + `"..."`。压成单行是因为笔记要嵌进系统提示的列表行里，原始换行会破坏渲染结构。

**`_as_bool`**（606–615 行）：宽容转 bool，`None` 用 default，字符串认 `1/true/yes/on`。模型传来的 `global` 参数可能是字符串 `"true"`（JSON 类型不严格时），严格 `bool(value)` 会把 `"false"` 判成 True——这是个真实的安全隐患，所以这里显式列白名单。

**`_format_tool_result`**（617–624 行）：`+` 标成功、`!` 标失败，回给模型的文本。

---
