# `core/session.py` 逐段讲解

> 本篇讲会话的持久化与树形结构。上游是 [loop.py](../../../core/loop.py)（主循环往会话里追加消息）和 REPL 命令层（`/tree`、`/fork`、`/compact`），下游是 [compact.py](compact.md)（判断摘要）与 [events.py](events.md)（时间戳/ID 复用）。

一个会话 = 工作区 `.wheel/sessions/<session_id>.jsonl` 里的**追加式日志**，逻辑上是一棵 `parent_id` 树；"当前对话"是从根走到 leaf 的那条路径。

- 行数：582 行
- 依赖：
  - [core/compact.py](compact.md) — `is_summary_item`，识别叠加层里的摘要消息
  - [core/events.py](events.md) — 复用 `_now()`（时间戳）和 `new_run_id()`（会话 ID）
  - [core/model.py](model.md) — `item_text` 从消息里取文本（预览用）
  - [core/plan.py](plan.md) — `PlanStore`，计划状态随 meta 一起持久化
  - [core/types.py](types.md) — `Item`、`Usage`
- 被谁用：
  - [core/loop.py](../../../core/loop.py)（[docs/loop-explained.md](../../loop-explained.md)）— `_push` 每追加一条消息就 `persist()`
  - [ui/app/commands.py](../ui/app-commands.md) — `/tree`、`/fork`、`/compact`
  - [ui/graph.py](../ui/graph.md) — 直接读 `session.entries` 画 DAG
  - [ui/app/refine.py](../ui/app-refine.md) — `invalidate_cache()`、`user_turns()`

## 目录

- [1. 常量与路径（1–37 行）](#1-常量与路径1–37-行)
- [2. 两个数据结构：节点与叠加层（39–57 行）](#2-两个数据结构节点与叠加层39–57-行)
- [3. `Session` 的状态字段（59–84 行）](#3-session-的状态字段59–84-行)
- [4. `create()` / `load()`：建与读（85–201 行）](#4-create--load建与读85–201-行)
- [5. 查找类方法（202–237 行）](#5-查找类方法202–237-行)
- [6. `append_item()`：往树上加节点（238–252 行）](#6-append_item往树上加节点238–252-行)
- [7. `path_ids()` / `view_items()`：路径即当前对话（253–274 行）](#7-path_ids--view_items路径即当前对话253–274-行)
- [8. `apply_compact()`：紧凑是叠加层（275–304 行）](#8-apply_compact紧凑是叠加层275–304-行)
- [9. 缓存纪元与失效（80–84、305–323 行）](#9-缓存纪元与失效80–84305–323-行)
- [10. `set_leaf()` / `fork()`：零拷贝分叉（324–339 行）](#10-set_leaf--fork零拷贝分叉324–339-行)
- [11. `tree_rows()`：把树渲染成行（340–377 行）](#11-tree_rows把树渲染成行340–377-行)
- [12. 空会话判定与清理（378–407 行）](#12-空会话判定与清理378–407-行)
- [13. `persist()`：增量追加 vs 全量重写（408–440 行）](#13-persist增量追加-vs-全量重写408–440-行)
- [14. 三种写盘操作（441–504 行）](#14-三种写盘操作441–504-行)
- [15. `user_turns()` 与三个模块级函数（505–582 行）](#15-user_turns-与三个模块级函数505–582-行)

## 函数/类速查表

| 名字 | 行号 | 职责 |
|---|---|---|
| `SESSION_DIR` / `CURRENT_VERSION` | 22 / 24 | 会话目录 `.wheel/sessions`；日志格式版本（v2 带 `parent_id`） |
| `session_dir()` | 32 | 会话目录路径（不存在则建） |
| `SessionEntry` | 40 | 树节点：一条消息 + `parent_id` |
| `CompactOverlay` | 49 | 紧凑叠加层：摘要 + 切点 `after_id` |
| `Session` | 59 | 会话完整状态：树 + 视图 + 叠加 + 计划 + 缓存纪元 |
| `Session.cache_key` | 81 | `f"{session_id}:{cache_epoch}"`，发给 provider 的缓存分区键 |
| `Session.create()` | 86 | 新建空会话（首次 `persist()` 才建文件） |
| `Session.load()` | 101 | 从 JSONL 恢复，含孤儿工具调用自愈 |
| `_session_files()` / `latest()` / `list_previews()` / `load_id()` | 203 / 208 / 216 / 227 | 会话文件的列举与查找 |
| `append_item()` | 238 | 追加一条消息到树上（`to_view` 控制是否同步进视图） |
| `path_ids()` | 253 | 根→leaf 的 ID 链 |
| `view_items()` | 267 | 当前视图（叠加层生效时返回摘要 + 后缀） |
| `apply_compact()` | 275 | 应用紧凑结果：设叠加层 + bump 纪元 |
| `invalidate_cache()` | 305 | 带外修改后 bump 纪元 + 全量重写 |
| `_sync_path_items()` / `_sync_ids()` | 311 / 316 | 把视图内容回写进树节点 |
| `set_leaf()` | 324 | 移动 leaf 指针（`fork`/`/tree` 跳转的核心） |
| `fork()` | 333 | 分叉（缺省从最后一个 user 消息） |
| `tree_rows()` | 340 | `/tree` 的行数据（深度、是否在路径上、是否 leaf） |
| `file_is_empty()` / `purge_empty()` | 379 / 395 | 空会话（只有头/meta）判定与清理 |
| `persist()` | 408 | 落盘：默认增量追加，必要时全量重写 |
| `_rebuild_linear()` / `_rewrite()` / `_entry_line()` / `_meta_line()` | 431 / 441 / 464 / 480 | 写盘的四个内部动作 |
| `user_turns()` | 505 | 真实用户轮数（摘要不算） |
| `preview_user_text()` | 510 | 会话列表预览文本（skill 展开缩成 `/skill:name`） |
| `unpaired_function_call_outputs()` | 534 | 找孤儿工具调用，造 interrupted 输出 |
| `first_user_preview_from_path()` | 560 | 不加载整个会话，扫文件取首条用户消息 |

---

## 1. 常量与路径（1–37 行）

```python
SESSION_DIR = ".wheel/sessions"
CURRENT_VERSION = 2
```

- `SESSION_DIR` 是**相对工作区**的路径，不是全局目录——会话跟着项目走，换目录就是另一个会话库。
- `CURRENT_VERSION = 2`：v2 的行里带 `parent_id`。v1 是线性日志（按写入顺序即链条），`load()` 会识别版本并按写入顺序重建 `parent_id`（见第 4 节）。版本号存在的意义是让老文件**可读**，而不是要求迁移。

`_nid()`（27–29 行）返回 8 位随机 hex，作为条目 ID。8 位不防碰撞，但只在一个会话文件内保证唯一，量级足够。

`session_dir()`（32–36 行）顺手 `mkdir(parents=True, exist_ok=True)`，调用方不用管目录存在性。

## 2. 两个数据结构：节点与叠加层（39–57 行）

```python
@dataclass
class SessionEntry:
    id: str
    parent_id: str | None
    item: Item
```

节点只有三个字段：自己的 ID、父 ID、消息本体。**没有子节点列表**——树是单向的（子指向父），所以"某个节点有哪些分支"只能靠全量扫描 `order` 反查（第 11 节 `tree_rows()` 就是这么干的）。代价是遍历，收益是分叉零成本：加一个分支只要新建一个 `parent_id` 指过去的节点。

```python
@dataclass
class CompactOverlay:
    summary: Item
    after_id: str
```

叠加层是**紧凑的全部持久状态**：一条摘要消息 + 一个切点。含义是"发给模型时，用 `summary` 替换 `after_id` 之前的全部历史"。原树一个字节都不动，所以跳分支、看旧内容仍然是完整的（第 8 节详述）。

## 3. `Session` 的状态字段（59–84 行）

字段分成三组理解：

**树与视图**（65–71 行）：

| 字段 | 含义 |
|---|---|
| `items` | **当前视图**，就是发给模型的那个列表 |
| `entries` | `id → SessionEntry`，全部节点（含不在当前路径上的分支） |
| `order` | 节点的写入顺序，持久化水位和 `/tree` 渲染都按它 |
| `leaf_id` | 当前叶子；当前对话 = 根→leaf 路径 |

`entries` 和 `items` 的关系是关键：`entries` 是全集，`items` 是路径上的子集（或叠加后的摘要 + 后缀）。`loop.py` 里 `items = session.items` 直接拿这个列表当别名用，所以循环里 append 一条消息，视图立刻就变了。

**持久化的可变状态**（69–78 行）：`turn_offset`（跨任务续计回合号）、`usage`（累计 token）、`header`、`cache_epoch`、`approvals`、`compactions`、`last_compact`、`plan`。这些都进每次 `persist()` 写的 meta 行（第 14 节）。

**内部水位**（79 行）：`_saved` 记录"已落盘的 `order` 长度"，增量追加时只写 `_saved:` 之后的条目。

`cache_key`（80–84 行）是 property 而非字段，因为它由 `session_id` 和 `cache_epoch` 派生：

```python
@property
def cache_key(self) -> str:
    return f"{self.session_id}:{self.cache_epoch}"
```

## 4. `create()` / `load()`：建与读（85–201 行）

**`create()`**（85–98 行）：只构造对象，不建文件。文件在首次 `persist()` 时才出现——避免"开了一堆会话但一条消息没说"留下的空文件（这类文件由 `purge_empty()` 清理，第 12 节）。会话 ID 直接复用 `new_run_id()`（时间戳 + 8 位随机），和时间顺序一致，文件名排序就是创建顺序。

**`load()`**（100–201 行）是这个文件里最长、也最有信息量的函数。流程：

1. 读全部非空行；空文件直接 `ValueError`。
2. **第一行必须是 header**，解析失败报 `corrupt session header`——头坏了就没法恢复，必须显式报错。
3. 取 `version = int(header.get("version") or 1)`，缺省当 v1。
4. 逐行解析（128–169 行）：
   - 单行 JSON 解析失败就 `continue`。注释写明了原因：`persist()` 在 write 与 fsync 之间崩了会留下半行，**跳过即可**，其他行都完好。
   - `type in {"item", "entry"}` 是消息节点。`item` 是老字段名，两个都认（兼容）。没有 `id` 就现场补一个 `_nid()`。
   - `parent_id` 缺失且 `version == 1` 时，用 `prev_id` 兜上，把线性日志重建成链。
   - 每个节点同时进 `entries`、`order`、`items`，并把 `prev_id`、`leaf_id` 前移。
   - `type == "meta"` 是状态快照，逐字段覆盖（后面出现的 meta 覆盖前面的——因为是追加式日志，最后的 meta 最新）。
5. 构造 `Session`（171–189 行），然后 `session.items = session.view_items()`（190 行）——**丢弃第 4 步攒的线性 `items`，改用路径 + 叠加层算视图**。这一步让"叶子可能在中间"的会话恢复正确。
6. **孤儿调用自愈**（191–193 行）：

```python
extras = unpaired_function_call_outputs(session.items)
if extras:
    for item in extras:
        session.append_item(item, to_view=True)
    session.persist()
```

崩溃发生在"模型要求调工具"和"工具结果落盘"之间时，文件里留下一条没有配对的 `function_call`。OpenAI Responses API 会拒这种序列（400），会话就再也续不下去。加载时补一条 `interrupted` 输出让序列合法，并**立即 `persist()`**——这样连读两次也不会重复补（`unpaired_function_call_outputs` 找不到未配对的了）。

补的文本不只是占位（548–556 行）：

```python
f"interrupted: {name} was dispatched and the outcome is unknown. "
"Do not blindly retry side-effecting tools; inspect the workspace first."
```

它明确告诉模型"结果未知，别盲目重试有副作用的工具，先看工作区"——这是给模型的指令，不只是给人的日志。

## 5. 查找类方法（202–237 行）

四个类方法都基于 `_session_files()`（203–205 行），它按 **mtime 倒序**返回 `*.jsonl`，所以"新的在前"。

- `latest()`（208–213 行）：返回最近一个**非空**会话，REPL 启动时自动恢复用它。跳过空文件的判断在 `file_is_empty()`。
- `list_previews()`（216–224 行）：`/sessions` 的列表。用 `first_user_preview_from_path()` 只扫文件不加载会话，预览文本为 `"(empty)"` 的行直接跳过——空会话不值得展示。
- `load_id()`（227–236 行）：按 ID 加载，文件不存在时走 glob 前缀匹配，**只有唯一命中才接受**，多个命中宁可 `FileNotFoundError` 也不猜。REPL 里 `/resume` 支持 `↑↓` 预览就是这个前缀匹配撑起来的。

## 6. `append_item()`：往树上加节点（238–252 行）

```python
eid = _nid()
node = SessionEntry(id=eid, parent_id=self.leaf_id, item=item)
self.entries[eid] = node
self.order.append(eid)
self.leaf_id = eid
if to_view:
    self.items.append(item)
```

新节点的父亲永远是**当前 leaf**，然后自己成为新 leaf——追加即沿当前分支生长。分叉时先 `set_leaf()` 移指针再追加，就长出了新分支（第 10 节）。

`to_view` 参数是为 [loop.py 的 `_push`](../../loop-explained.md)准备的：`run_agent` 里 `items` 就是 `session.items` 本身（别名），调用方已经 append 过了，所以传 `to_view=False`，避免同一条消息在视图里出现两次。**树永远记，视图按调用方需要记。**

## 7. `path_ids()` / `view_items()`：路径即当前对话（253–274 行）

```python
def path_ids(self) -> list[str]:
    ids: list[str] = []
    cur = self.leaf_id
    seen: set[str] = set()
    while cur and cur not in seen:      # 带环保护
        seen.add(cur)
        if cur not in self.entries:
            break
        ids.append(cur)
        cur = self.entries[cur].parent_id
    ids.reverse()
```

从 leaf 沿 `parent_id` 往回走到根，再反转。`seen` 集合是**环保护**：脏数据（手工编辑过的文件）出现 `A.parent = B, B.parent = A` 时不会死循环。

```python
def view_items(self) -> list[Item]:
    ids = self.path_ids()
    if self.overlay and self.overlay.after_id in ids:
        idx = ids.index(self.overlay.after_id)
        return [self.overlay.summary, *[self.entries[i].item for i in ids[idx:]]]
    return [self.entries[i].item for i in ids]
```

视图 = 路径上的消息；有叠加层且切点还在路径上时，切点之前的全部历史换成一条摘要。注意**切点本身（`after_id`）保留**——它是保留后缀的第一条，摘要只替换它之前的。

## 8. `apply_compact()`：紧凑是叠加层（275–304 行）

这是整个模块的设计核心。紧凑结果 `compacted` 有两种形态，分别处理：

**形态一：没有摘要（283–286 行）**——`compact_history` 对极小会话是空操作，或只做了路径同步。此时把视图内容同步回树（`_sync_path_items`）并原地替换 `self.items`。

**形态二：有摘要**（287–304 行）：

1. 取 `compacted[0]` 作摘要，`compacted[1]` 是保留后缀的第一条。
2. **反查 `after_id`**（291–296 行）：从路径末尾往回找，哪个节点的 `item` 是（或等于）`compacted[1]`，那个节点的 ID 就是切点。用 `is` 先比、`==` 后比——同一个对象最快，内容相等也接受。
3. 找不到时（297–298 行）退化为 `path_ids()[-1]`，即叠加到全路径之后（等价于全部历史都被摘要替换）。
4. 设 `overlay`，然后用 `_sync_ids(ids[idx:], compacted[1:])` 把后缀的消息同步回树节点，最后 `cache_epoch += 1`。

```python
self.overlay = CompactOverlay(summary=summary, after_id=after_id)
ids = self.path_ids()
idx = ids.index(after_id)
self._sync_ids(ids[idx:], compacted[1:])
self.cache_epoch += 1
self.items[:] = compacted
```

**为什么不销毁原树？** 三个理由：

1. **分叉回退**：`/tree <旧节点>` 跳回去时，路径变了，叠加层可能不再适用（第 10 节），此时需要原始消息重建视图。
2. **审计/replay**：[events.py](events.md) 里录的是原始响应，会话文件保留原始消息才能和事件流对照。
3. **叠加层是可撤销的**：切点只是一个 ID 指针，丢掉它就回到完整历史，没有任何破坏性操作。

代价：紧凑后文件不会变小（原树还在），`persist()` 在有 overlay 时必须**全量重写**而不是追加——下次展开讲。

## 9. 缓存纪元与失效（80–84、305–323 行）

前缀缓存（prompt cache）的规则是：provider 按"请求前缀"缓存，前缀变了缓存就废。所以：

- 普通回合只是**追加**消息，前缀不变 → `cache_epoch` 不动 → `cache_key` 不变 → 缓存持续命中。
- 一旦历史被改写（紧凑、分支跳转、refine 编辑），前缀变了 → `cache_epoch += 1` → `cache_key` 变成新值 → provider 在新分区重新积累缓存，**旧分区不会被错误复用**。

`invalidate_cache()`（305–309 行）是给"带外修改"用的入口：

```python
def invalidate_cache(self) -> None:
    self.cache_epoch += 1
    self.persist(rewrite=True)
```

[ui/app/refine.py](../ui/app-refine.md) 在应用了 harness 编辑后调它（harness 笔记进 system prompt，前缀必然变了）。`rewrite=True` 是因为纪元是持久状态，必须落盘，否则下次加载会话又会用回旧键。

`cache_key` 发给 provider 的路径：[loop.py 的 `_sync_cache_key`](../../loop-explained.md) 在每次模型调用前把 `session.cache_key` 拷到 model 客户端上。

`_sync_path_items()` / `_sync_ids()`（311–322 行）是视图→树的回写：逐 ID 对齐，把视图里的 `item` 写回对应节点的 `item`，找不到节点就跳过。`_sync_path_items` 在已有 overlay 时直接 return——**叠加层的摘要不该覆盖原树节点**（覆盖就破坏了"不销毁原树"的前提）。

## 10. `set_leaf()` / `fork()`：零拷贝分叉（324–339 行）

```python
def set_leaf(self, entry_id: str) -> None:
    if entry_id not in self.entries:
        raise KeyError(f"unknown entry {entry_id}")
    self.leaf_id = entry_id
    if self.overlay and self.overlay.after_id not in self.path_ids():
        self.overlay = None       # 跳出了叠加层生效范围
    self.items[:] = self.view_items()
```

移动 leaf 指针 = 换一条根到叶的路径 = 换一段"当前对话"。**没有任何消息被复制或删除**，这就是 README 里说的"分叉零拷贝"。

叠加层的处理（329–330 行）：切点 `after_id` 不在新路径上时，叠加层失效（丢弃）。因为摘要描述的是旧路径的历史，套到另一个分支上是错的。丢弃后 `view_items()` 自然返回完整的原始历史——**这就是叠加层不销毁原树的直接收益**。

`fork()`（333–338 行）是 `set_leaf()` 的语义包装：不给参数时默认分叉到**路径上最后一个 user 消息**（`_last_user_id()`，371–376 行）。选 user 消息作切点是刻意的：从助手消息中间分叉会产生半截对话，从 user 消息分叉才是"从这里重新问一遍"。

`/tree` 跳转走的就是这条路（[commands.py](../ui/app-commands.md) 的 `handle_tree`）：`session.fork(spec)` → `session.persist(rewrite=True)`。

## 11. `tree_rows()`：把树渲染成行（340–377 行）

`/tree` 的行数据生成。三个细节：

**只列 user 消息**（346–347 行）：助手消息和工具结果是 user 回合的附属，列出来会让树变成几十行噪音。一行 = 一个用户回合。

**摘要行标成 `(summary)`**（350–353 行）：叠加层的摘要也是 `role: user` 消息（compact 注入时伪装的），不特殊处理会显示成一长串压缩内容。

**深度靠逐级回溯算**（354–360 行）：

```python
depth = 0
cur = node.parent_id
while cur and cur in self.entries:
    if self.entries[cur].item.get("role") == "user":
        depth += 1
    cur = self.entries[cur].parent_id
```

因为只有父指针、没有子指针，深度只能往回走。而且**只数 user 祖先**——这样深度和"第几轮对话"对上，而不是和消息条数对上。

`leaf` 字段（365 行）的判定有个兜底：`eid == self.leaf_id or (self.leaf_id in path and eid == self._last_user_id())`。leaf 常常落在助手消息上，但树只显示 user 行，所以要退回到"路径上最后一个 user 消息"才算当前位置。

## 12. 空会话判定与清理（378–407 行）

`file_is_empty()`（378–392 行）的定义是：**文件里没有任何消息条目**（只有 header / meta 也算空）。实现上逐行 JSON 解析，坏行跳过，只要发现一条 `type in {"item", "entry"}` 就返回 `False`。读不到文件时返回 `False`（不敢删）。

`purge_empty()`（394–407 行）遍历删除，`OSError` 静默跳过（文件被占用时不炸）。返回值是删除数，[ui/app/\_\_init\_\_.py](../ui/app.md) 在启动时调一次，避免反复开会话积累垃圾文件。

## 13. `persist()`：增量追加 vs 全量重写（408–440 行）

```python
def persist(self, rewrite: bool = False) -> None:
    if not self.entries and not self.items:
        return
    if not self.entries and self.items:
        self._rebuild_linear()                      # 旧数据：items → 线性树
    elif self.entries and self.items and self.overlay is None:
        self._sync_path_items(self.items)
    if rewrite or not self.path.exists() or self.overlay is not None:
        self._rewrite()
        return
    with self.path.open("a", encoding="utf-8") as fh:
        unsaved = self.order[self._saved :] if self._saved <= len(self.order) else self.order
        for eid in unsaved:
            fh.write(self._entry_line(self.entries[eid]))
        fh.write(self._meta_line())
        fh.flush()
        os.fsync(fh.fileno())
    self._saved = len(self.order)
```

**默认走增量追加**：只写 `_saved` 水位之后的新条目，再补一条 meta（meta 是完整状态快照，每次覆盖式重放——加载时最后一条生效）。`loop.py` 里每次 `_push` 都调 `persist()`，靠水位保证不会把整棵树重写一遍。

**三个条件触发全量重写**（418–420 行）：

| 条件 | 原因 |
|---|---|
| `rewrite=True` | 调用方明确要求（紧凑、`/tree` 跳转、`invalidate_cache`） |
| 文件不存在 | 首次落盘，得先写 header |
| `overlay is not None` | 有叠加层时，meta 里的 overlay 是**覆盖语义**；只追加会让旧 meta（无 overlay 或旧 overlay）残留在中间，且叠加点是"历史变形"，纯追加表达不了 |

最后一句解释为什么 overlay 必须重写：追加式日志只能**长大**，不能**变形**。紧凑把"历史"改成了"摘要 + 后缀"，这是变形，只能重写。

**崩溃安全**：写后 `flush()` + `os.fsync()`，强制操作系统把缓冲区落盘。代价是每次工具调用多一次磁盘同步（README 里明说了这个取舍），收益是掉电不丢已确认的记录。最坏情况只留一行写了一半的 JSON，读侧跳过（第 4 节）。

`_saved` 水位在 `_rebuild_linear` 或增量写之后都更新为 `len(self.order)`，保证重写后继续追加不会重复落盘。

## 14. 三种写盘操作（441–504 行）

**`_rebuild_linear()`**（431–440 行）：把裸 `items` 列表重建成一条链（`parent_id` 串起来，`leaf_id` 指到最后一个）。这是给"直接往 `Session.items` 里塞数据"的旧调用路径兜底的——树结构缺失时补建，而不是报错。

**`_rewrite()`**（441–462 行）：拼 `[header, *entries, meta]` 一次性 `write_text`，然后以二进制模式重开文件 `fsync`（`write_text` 不暴露 fd，只能重开一次）。header 每次重写都刷新 `version` / `id` / `cwd`，`timestamp` 保留原值（450 行的 `header.get("timestamp") or _now()`）。

**`_entry_line()`**（464–478 行）：一行一个节点，`type: "entry"`，带 `timestamp`。每行是完整 JSON——崩溃不会撕坏别的行。

**`_meta_line()`**（480–503 行）：尾部 meta 行的字段清单，就是"会话的全部可变状态"：

| 字段 | 说明 |
|---|---|
| `turn_offset` | 已用回合数，跨任务续计 |
| `usage` | 累计 token（`Usage.as_dict()`） |
| `leaf_id` | 当前叶子，决定恢复后的"当前对话" |
| `overlay` | 紧凑叠加层（summary + after_id） |
| `cache_epoch` | 缓存纪元 |
| `compactions` / `last_compact` | 紧凑次数与上次统计 |
| `approvals` | **本会话已批准过的 bash 意图**（`list[list[str]]`） |
| `plan` | 计划步骤与确认/拒绝状态 |

`approvals` 存的是 `list[list[str]]` 而不是 `list[tuple[str, ...]]`，因为 JSON 会把 tuple 序列化成数组、反序列化回 list——**用 list 存才和读回来的一致**。它的消费路径：[loop.py](../../../core/loop.py) 建默认安全门时把 `session.approvals` 转成 `memory` 集合传给 [SafetyGate](../tools/safety.md)，实现"同一意图一个会话只确认一次"；跑完再 `[list(k) for k in safety.memory]` 写回（loop.py 251、360–361 行）。

## 15. `user_turns()` 与三个模块级函数（505–582 行）

**`user_turns()`**（505–508 行）：统计 `role == "user"` 且**不是摘要**的消息数。排除摘要是必要的——摘要消息伪装成 user 消息，算进去会让"第几轮"虚高。loop.py 用它初始化 `display_turn`（[loop-explained 第 4 节](../../loop-explained.md)）。

**`preview_user_text()`**（510–531 行）：会话列表的预览文本。特殊处理 skill 展开：`<skill name="foo">…</skill>` 这种被展开进 user 消息的内容会占满预览，所以缩成 `/skill:foo`，后面接 skill 标签之外的正文（522–525 行）。最后做空白归一化和 `width` 截断（默认 48），超长加 `…`。

**`unpaired_function_call_outputs()`**（534–557 行）：扫一遍消息，用 `pending` 字典记录未配对的 `function_call`（遇到对应的 `function_call_output` 就 pop 掉），剩下的全是孤儿。为每个孤儿造一条 `function_call_output`，`output` 是那段 interrupted 提示（第 4 节）。

**`first_user_preview_from_path()`**（560–582 行）：只 `read_text` 扫行、不构造 `Session`，用于 `/sessions` 列表。读失败返回 `"(unreadable)"`，没有 user 消息返回 `"(empty)"`——而 `list_previews()` 正是靠 `"(empty)"` 这个哨兵值跳过空会话。
