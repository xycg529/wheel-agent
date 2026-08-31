# `ui/graph.py` 逐段讲解

> 本篇讲 `ui/graph.py`。上游是 [`ui/app/commands.py`](../../../ui/app/commands.py) 的 `handle_graph`（`/graph` 命令）和 [`ui/app/__init__.py`](../../../ui/app/__init__.py) 的命令分发与退出清理；下游是 [`core/session.py`](../../../core/session.py)（会话树）、[`tools/audit.py`](../../../tools/audit.py)（脱敏）、[`core/model.py`](../../../core/model.py)（`item_text`）、[`tools/tools.py`](../../../tools/tools.py)（`parse_function_calls`）。

把会话树渲染成两种可视化：**终端 ASCII 图**（`/graph`，box-drawing 字符画的盒子和分叉横杆）和**自包含 HTML 页**（`/graph html`，写文件 + 起本地 HTTP 服务）。图上只画对话流（user / say / tool），分支按列并排，当前路径高亮。

- 行数：698 行
- 依赖：
  - [`core/session.py`](../../../core/session.py) — `Session` 的 `entries/order/leaf_id/path_ids()` 构成树；`preview_user_text` 做文本预览
  - [`core/compact.py`](../../../core/compact.py) — `is_summary_item`，把摘要消息排除在 user 节点之外
  - [`core/model.py`](../../../core/model.py) — `item_text`，取一条消息的文本（content 可能是分片列表）
  - [`tools/tools.py`](../../../tools/tools.py) — `parse_function_calls`，把 `function_call` item 解析成 `FunctionCall`
  - [`tools/audit.py`](../../../tools/audit.py) — `redact_tool_args` / `redact_tool_output`，图可能写进 HTML 文件落盘，必须先脱敏
- 被谁用：
  - [`ui/app/commands.py`](../../../ui/app/commands.py) — `handle_graph` 调 `build_session_graph` / `render_ascii` / `write_html` / `serve_graphs`
  - [`ui/app/__init__.py`](../../../ui/app/__init__.py) — 导入 `stop_graph_server`，在退出路径（391 行）关掉 HTTP 服务

> **先纠一个容易搞混的点**：`/tree` 不走这个模块。`/tree` 用的是 `Session.tree_rows()`（会话里每个 user 消息一行，带深度）+ `pick_list` 方向键选择器，画的是缩进列表；`/graph` 才走 `graph.py`，画的是**带框的盒子 + 分叉横杆的 DAG 图**。两者共享"当前路径"概念，但数据结构和渲染完全独立。

## 目录

- [1. 模块定位与 `PARALLEL_TOOLS`（1–33 行）](#1-模块定位与-parallel_tools1–33-行)
- [2. 数据结构：`GraphNode` / `GraphLayer` / `GraphBlock` / `SessionGraph`（35–85 行）](#2-数据结构graphnode--graphlayer--graphblock--sessiongraph35–85-行)
- [3. `build_session_graph`：从会话树建图（87–194 行）](#3-build_session_graph从会话树建图87–194-行)
- [4. `_flatten_path`：线性视图（196–210 行）](#4-_flatten_path线性视图196–210-行)
- [5. 节点构造：`_entry_node` / `_tool_node`（212–250 行）](#5-节点构造_entry_node--_tool_node212–250-行)
- [6. `render_ascii` 总入口（252–266 行）](#6-render_ascii-总入口252–266-行)
- [7. 布局算法：`_render_block` 与 `_fork_bar`（282–313 行）](#7-布局算法_render_block-与-_fork_bar282–313-行)
- [8. 字符画：`_box` / `_row` / `_label`（526–562 行）](#8-字符画_box--_row--_label526–562-行)
- [9. HTML 渲染：`render_html` / `_html_*`（315–427、564–627 行）](#9-html-渲染render_html--_html_315–427564–627-行)
- [10. `write_html` 与 `list_session_runs`（429–436、629–654 行）](#10-write_html-与-list_session_runs429–436629–654-行)
- [11. HTTP 服务：`serve_graphs` / `stop_graph_server`（656–698 行）](#11-http-服务serve_graphs--stop_graph_server656–698-行)
- [12. 与 `/tree`、`/graph` 命令的配合](#12-与-treegraph-命令的配合)

## 函数/类速查表

| 名字 | 行号 | 职责 |
|---|---|---|
| `PARALLEL_TOOLS` | 24 | 可并行的只读工具集合；相邻同类调用在图上并排一行 |
| `GraphNode` | 36 | 图上一个节点（user/say/tool） |
| `GraphLayer` | 51 | 一层：同一时刻的并列节点（如并行工具调用） |
| `GraphBlock` | 58 | 一段：若干层 + 若干分支（递归结构，整棵图的骨架） |
| `SessionGraph` | 77 | 建图产物：线性视图 `layers` + 完整结构 `tree` + runs + leaf |
| `build_session_graph` | 87 | 主入口：会话树 → `SessionGraph` |
| `_flatten_path` | 196 | 把路径上的分支压平成层列表（线性视图） |
| `_entry_node` | 212 | 条目 → user/say 节点 |
| `_tool_node` | 231 | `function_call` → 工具节点（参数/结果脱敏） |
| `render_ascii` | 252 | 渲染 ASCII（`/graph` 用） |
| `_count_leaves` | 268 | 数一段有多少叶子 = 分支数 |
| `_tree_has_off` | 275 | 图里是否有不在路径上的节点（决定画不画 `*`） |
| `_render_block` | 282 | **布局核心**：递归渲染一段，多分支按列并排 |
| `_fork_bar` | 306 | 分叉横杆（每列中间一个 `\|`） |
| `render_html` | 315 | 渲染自包含 HTML（内联 CSS，无外部依赖） |
| `write_html` | 429 | 写到 `.wheel/graphs/<session_id>.html` |
| `_is_user` / `_is_assistant` | 438 / 443 | 条目类型判定（摘要消息不算 user） |
| `_user_node` / `_assistant_node` | 450 / 464 | 建 user / say 节点 |
| `_tool_groups` | 475 | 工具节点分组：连续并行类并成一组 |
| `_status` | 503 | 结果状态：`blocked` / `error` / `ok` |
| `_args_preview` | 512 | 工具参数压成 `key: value` 多行预览 |
| `_box` | 526 | 一个节点 → 带框 ASCII 盒 |
| `_row` | 542 | 几个节点并排一行 |
| `_label` | 555 | 节点标题行（路径上的带 `*`，工具带状态） |
| `CODE_ARG_KEYS` | 565 | HTML 里用 `<pre>` 展示的参数名（多行/代码） |
| `_html_args` / `_html_card` / `_html_block` | 568 / 583 / 609 | HTML 的参数块、单卡片、递归段渲染 |
| `list_session_runs` | 629 | 列出属于该会话的全部 run ID |
| `serve_graphs` | 661 | 起本地 HTTP 服务托管图文件，返回 URL |
| `stop_graph_server` | 689 | 关掉 HTTP 服务 |

---

## 1. 模块定位与 `PARALLEL_TOOLS`（1–33 行）

模块 docstring 三句话定调：图上只画**对话流**（user/say/tool），工具输出、思考、空 assistant 文本作为**透传节点跳过**，分支按列并排，当前路径高亮。

导入里有个值得注意的点：`from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer` 和 `atexit`、`threading` —— 这个"渲染模块"还兼职起一个本地 HTTP 服务（第 11 节）。选标准库而不是第三方（如 Flask）是为了守住项目"运行时依赖只有 `openai` + `python-dotenv`"的约束。

`PARALLEL_TOOLS`（24–32 行）是一份**硬编码白名单**：

```python
PARALLEL_TOOLS = {"read", "ls", "grep", "glob", "web_search", "web_fetch", "bash_poll"}
```

判断依据是工具语义而非实际调度：这些全是只读工具，模型常在一次回复里连发好几个 `read`/`grep`。图上把它们**并排画成一行**（而不是竖着叠成一串盒子），高度立刻从 N 个盒子压成 1 行，图才不至于被"读五个文件"拉成三屏。

注意这份名单和 [`tools/safety.py`](../../../tools/safety.py) 的 `READ_ONLY` 是**两处独立维护的名单**（后者多一个 `bash_poll` 之外的差异，前者没有 `bash`）。两处语义不同（一个是安全裁决、一个是排版），但新增只读工具时要记得同步改这里，否则图上会退化成竖排。

## 2. 数据结构：`GraphNode` / `GraphLayer` / `GraphBlock` / `SessionGraph`（35–85 行）

四个 dataclass 构成"图"的中间表示，渲染层（ASCII 和 HTML）都只认这一层，不碰会话条目。

**`GraphNode`（36–48 行）** — 一个盒子。`kind`（`user`/`assistant`/`tool`）决定 HTML 的边框配色和 ASCII 的标题前缀；`title` 是标题行（`user 3` / `say` / `read`）；`detail` 是盒内预览（参数或文本）；`result` 只有工具节点有，ASCII 里渲染成 `→ 预览` 一行，HTML 里进 `<details>`；`status` 是 `ok`/`error`/`blocked`；`args` 只在 HTML 路径上用（ASCII 用 `detail` 里的文本预览就够）；`on_path` 是**是否在当前路径上**——整张图的高亮就靠这一个布尔量传递。

**`GraphLayer`（51–55 行）** — 一层 = 同一时刻并列的一批节点。单节点的层画一个盒子，多节点的层用 `_row` 并排。

**`GraphBlock`（58–74 行）** — 图的骨架，递归结构：

```python
@dataclass
class GraphBlock:
    layers: list[GraphLayer] = field(default_factory=list)
    branches: list[GraphBlock] = field(default_factory=list)
```

语义是"**一段**"：先顺序若干层，末尾接若干分支（每个分支又是一段）。会话树里的**分叉**（同一父节点有多个可见子节点）在这里变成 `branches`；线性部分（单子节点）被压进同一段的 `layers`，不额外建段——这让"大部分会话没有分叉"这种常见情况的结构保持扁平。

两个便捷属性：

- `on_path`（65–70 行）：段内任一节点在路径上，或任一分段在路径上。递归判定，HTML 用它在分支 `<div>` 上加 `on-path`/`off-path` class。
- `empty()`（72–74 行）：无层无分支。`render_ascii` 和 `render_html` 都用它做"树为空时退回线性视图"的判断。

**`SessionGraph`（77–85 行）** — 建图产物，同时保留两个视图：

```python
layers: list[GraphLayer]     # 线性视图：把路径上的分支压平成层
tree: GraphBlock             # 完整分支结构
runs: list[str]              # 属于该会话的 run ID
leaf_id: str | None          # 当前叶子（header 里显示 now <id>）
```

`layers` 的注释点明分工：header 里的 `layers=` 计数和"图是否为空"的判定用 `layers`；真正画出来的是 `tree`，因为**分叉是图的核心价值**——只看当前路径的话，`/graph` 就退化成一份对话列表了。

## 3. `build_session_graph`：从会话树建图（87–194 行）

`build_session_graph` 是唯一对外暴露的建图入口。整函数分四步：建索引 → 定"可见性"规则 → 递归建段 → 组装产物。

**第一步：父子索引与路径集合**（88–107 行）

```python
kids: dict[str | None, list[str]] = {}
for eid in session.order:
    node = entries.get(eid)
    kids.setdefault(node.parent_id, []).append(eid)
path = set(session.path_ids())
```

会话文件里只存 `parent_id`（见 [`core/session.py`](../../../core/session.py)），没有子指针，所以第一件事是**反建 children 索引**。按 `session.order`（写入顺序）遍历，保证 `kids` 里每个列表是时间有序的——图上的左右列顺序由此决定：先写的分支在左。

`path` 是 `session.path_ids()`（根→leaf 的 ID 链）转成的集合，供后面逐节点打 `on_path` 标记。

同一次循环里还顺带收集三样东西：

- `outputs: dict[call_id, output]` 和 `errors: dict[call_id, bool]` — 工具结果和是否出错。会话树里 `function_call` 和 `function_call_output` 是两个独立节点，建工具节点时要靠 `call_id` 把结果找回来配对。
- `user_n: dict[eid, n]` — 每个 user 消息是第几轮（跳过摘要消息后计数）。标题做成 `user 3` 而不是 `user`，是为了和 `/tree` 里"第几条用户消息"对上，方便跳转。

**第二步：`passthrough` — 什么不画**（109–118 行）

```python
def passthrough(item):
    kind = item.get("type")
    if kind in {"function_call_output", "reasoning", "thinking"}:
        return True
    if kind == "function_call" or _is_user(item):
        return False
    if _is_assistant(item):
        return not item_text(item).strip()
    return True
```

注释解释了设计意图：图画的是**对话流，不是原始 API item 列表**。工具输出、思考块、以及"只有工具调用没有文本"的 assistant 消息都不带流程信息，画出来只会让图长高一倍且全是噪音。

空 assistant 文本这条尤其重要：模型每轮回复几乎都有一个空文本 item 加若干 `function_call`，不跳过的话图上会出现一堆空盒子。

**第三步：`visible_kids` — 透传节点的递归提升**（120–128 行）

```python
def visible_kids(eid):
    found = []
    for cid in kids.get(eid, []):
        item = entries[cid].item
        if passthrough(item):
            found.extend(visible_kids(cid))   # 穿过它，把它的子节点提上来
        else:
            found.append(cid)
    return found
```

透传节点不是简单丢弃，而是把它**从链路上摘掉、把它的子节点接到它父节点下面**。这样做的后果：`visible_kids` 是递归的，深度等于连续透传节点链的长度——长会话里一次递归几层很常见，Python 默认递归上限 1000 对"每个工具调用都跟一个输出"的结构足够，但会话里出现几千层连续透传时会 `RecursionError`（见第 13 节）。

**第四步：`gather_tools` / `build_from` — 建段**（130–160 行）

```python
def gather_tools(start):
    batch = [start]
    cur = start
    while True:
        direct = kids.get(cur, [])
        if len(direct) == 1 and entries[direct[0]].item.get("type") == "function_call":
            cur = direct[0]
            batch.append(cur)
            continue
        return batch, cur
```

`gather_tools` 把**一串单子节点的 `function_call`** 收进一个 batch。为什么需要它：模型一次回复里的并行工具调用，在树上是"父 → 调用A → 调用B → 调用C"的链（每次 `append_item` 都把新节点挂在当前 leaf 下），不是"父 → A/B/C"的扇出。不合并的话，三个并行 `read` 会被画成三个**竖着叠**的盒子，看起来像串行执行——与事实相反。所以先把链收成 batch，再交给 `_tool_groups`（第 5 节）按 `PARALLEL_TOOLS` 分成并排组。

```python
def build_from(eid):
    layers = []
    cur = eid
    while cur:
        item = entries[cur].item
        if item.get("type") == "function_call":
            batch, last = gather_tools(cur)
            nodes = [_tool_node(i, ...) for i in batch]
            for group in _tool_groups(nodes):
                layers.append(GraphLayer(group))
            nxt = visible_kids(last)
        else:
            node = _entry_node(cur, ...)
            if node is not None:
                layers.append(GraphLayer([node]))
            nxt = visible_kids(cur)
        if not nxt:
            return GraphBlock(layers=layers)
        if len(nxt) == 1:
            cur = nxt[0]
            continue
        return GraphBlock(layers=layers, branches=[build_from(x) for x in nxt])
    return GraphBlock(layers=layers)
```

`build_from` 沿单子节点链往下走，把每层塞进同一个 `GraphBlock.layers`；**遇到 `len(nxt) > 1` 就是分叉点**，当前段到此结束，为每个子节点递归建一个分支段。这就是"线性部分压平、分叉处才建新段"的实现。

**组装产物**（162–194 行）：根节点本身也可能是透传节点（比如会话开头是一条工具输出），所以先对 `kids[None]` 做一次同样的展开，再按可见根的数量决定是单根直建、多根建并列分支、还是空图。最后返回 `SessionGraph`，`layers` 由 `_flatten_path(tree)` 得出。

## 4. `_flatten_path`：线性视图（196–210 行）

```python
def _flatten_path(block):
    out = []
    for layer in block.layers:
        nodes = [node for node in layer.nodes if node.on_path]
        if nodes:
            out.append(GraphLayer(nodes))
    on = [branch for branch in block.branches if branch.on_path]
    if len(on) == 1:
        out.extend(_flatten_path(on[0]))
    else:
        for branch in on:
            out.extend(_flatten_path(branch))
    return out
```

只保留 `on_path` 的节点和分支，得到"当前对话"的层列表。两个分支都在路径上时（`len(on) != 1`，理论上不该发生，因为路径唯一）两个都接上——**宁可多画不错画**。

用途有三：header 的 `layers=N` 计数、`/graph` 的空会话判定（`not graph.layers and graph.tree.empty()`），以及 `render_ascii` 在树为空时退回的线性视图。

## 5. 节点构造：`_entry_node` / `_tool_node`（212–250 行）

**`_entry_node`（212–229 行）** — 分派到 `_user_node` 或 `_assistant_node`；空 assistant 文本返回 `None`（`build_from` 里判空后不建层）。

**`_user_node`（450–462 行）**：标题 `user {n}`，`detail` 是 `preview_user_text(text, 120)`（120 字符预览，换行压缩成空格），`body` 存全文给 HTML 用。**摘要消息不算 user**（`_is_user` 排除了 `is_summary_item`）——紧凑产生的摘要是合成节点，画成 `user N` 会误导编号。

**`_assistant_node`（464–473 行）**：标题统一是 `say`，不显示 ID 或序号——模型每轮的发言在图上是"一次说话"，编号只会增加噪音。`detail` 用 160 字符预览（比 user 长，因为 assistant 文本常带结论）。

**`_tool_node`（231–250 行）** — 关键在**脱敏发生在建节点时，不是渲染时**：

```python
call = calls[0]
args = redact_tool_args(call.name, call.arguments)
raw = outputs.get(call.call_id, "")
result = redact_tool_output(call.name, call.arguments, raw)
```

为什么必须在这里脱敏：`/graph html` 会把整页写进 `.wheel/graphs/<id>.html` 落盘，还会起 HTTP 服务。对敏感路径（`.env`、`id_rsa` 等，见 [`tools/audit.py`](../../../tools/audit.py)）的 `write`/`edit` 参数、`read`/`web_fetch` 输出，必须在这层就换成 `<redacted>`——渲染层（两个）不用各自记得脱敏，是**单点防御**。

解析失败的调用（`parse_function_calls` 返回空）退化成 `GraphNode(kind="tool", title="tool")`，不丢节点。

**`_status`（503–510 行）** 有个值得学的判断：

```python
def _status(result, is_error=False):
    low = result.lower()
    if "blocked by safety" in low:
        return "blocked"
    return "error" if is_error else "ok"
```

`blocked` 只能从输出文本里嗅探（会话树只存原始 item，`ToolResult.blocked` 没有落盘），但**不能用同样的办法嗅探 `error`**——注释点明原因：`read error.log`、`grep error` 这类合法结果里就带 "error" 字样，会被误标成失败。所以错误状态只信任循环存下来的结构化 `is_error`。

**`_tool_groups`（475–501 行）** — 把 batch 切成组：连续的 `PARALLEL_TOOLS` 成员并成一组（并排画），非并行工具单独成组（独占一行）。状态机用 `parallel: bool | None` 三态（未定/并行中/串行），逻辑偏绕但避免了"一个 `bash` 混在三个 `read` 中间"画错的情形。

**`_args_preview`（512–524 行）** — 参数压成 `key: value` 多行；非字符串值 `json.dumps`；空白折叠成单空格；单值超 80 字符截断成 `77 + "..."`（77 而非 80，是给省略号留位置）。

## 6. `render_ascii` 总入口（252–266 行）

```python
def render_ascii(graph, *, width=56):
    block = graph.tree if not graph.tree.empty() else GraphBlock(layers=graph.layers)
    mark = _tree_has_off(block)
    n_branch = max(1, _count_leaves(block)) if not block.empty() else 0
    header = f"session {graph.session_id}  layers={len(graph.layers)}  branches={n_branch}"
    if graph.leaf_id:
        header += f"  now {graph.leaf_id}"
    lines = [header]
    if graph.runs:
        lines.append("runs  " + ", ".join(graph.runs))
    lines.append("")
    lines.extend(_render_block(block, width, mark_path=mark))
    return "\n".join(lines).rstrip() + "\n"
```

- **优先画 `tree`**（含分叉），只有树为空时才退回 `layers`（线性视图）。
- `mark_path=mark`：`mark` 来自 `_tree_has_off`（275–280 行）——**只有图里存在不在路径上的节点时才画 `*` 标记**。无分叉的会话全在路径上，满屏 ` *` 是噪音；有分叉时 `*` 才承担"这是当前路径"的信息。
- `branches` 计数用 `_count_leaves`（268–273 行）：叶子数即分支数，最小取 1（单链会话报 1 而不是 0）。
- header 里带 `runs`（该会话对应的 run ID 列表，供 `/replay` 直接取用）和 `now <leaf_id>`（当前位置，和 `/tree` 的跳转目标同一套 ID）。

`width=56` 是默认值，但 `handle_graph` 调用时**不传 width**，所以终端里永远是 56 列——没有按终端宽度自适应（见第 13 节）。

## 7. 布局算法：`_render_block` 与 `_fork_bar`（282–313 行）

布局是**自己算的字符网格**，没有用任何树布局算法（Reingold-Tilford 之类）。做法是"**渲染即布局**"：每个子段先渲染成自己的行列表，父段把子段的行列表**按列拼接**。

```python
def _render_block(block, width, *, mark_path):
    lines = []
    for i, layer in enumerate(block.layers):
        if i:
            lines.append("          |")
        if len(layer.nodes) == 1:
            lines.extend(_box(layer.nodes[0], width, mark_path=mark_path))
        else:
            lines.extend(_row(layer.nodes, width, mark_path=mark_path))
    if not block.branches:
        return lines
    n = len(block.branches)
    col_w = max(16, (width - 2 * (n - 1)) // n)
    cols = [_render_block(branch, col_w, mark_path=mark_path) for branch in block.branches]
    if lines:
        lines.append(_fork_bar(n, col_w))
    height = max((len(col) for col in cols), default=0)
    padded = [col + [" " * col_w] * (height - len(col)) for col in cols]
    for row in zip(*padded):
        lines.append("  ".join(row))
    return lines
```

四个要点：

1. **层间连线**（286–287 行）是硬编码的 `"          |"` —— 10 个空格加一个竖线，位置大致对着单盒子（56 宽）的中间。分叉后列宽变小，这根竖线不会跟着移到列中心，所以分支内部的连线会和盒子错位。这是字符画方案的已知粗糙处：连线位置是常量而非计算值。
2. **列宽分配**（295 行）：`col_w = max(16, (width - 2 * (n - 1)) // n)`。总宽减去列间距（`n-1` 个两空格的间隔）后平分；`max(16, ...)` 是**下限保护**——分支多了（比如 5 个分支、56 宽）算出来只有 10 列，盒子会窄到只剩边框，所以强制至少 16。代价是总宽超限（见第 13 节）。
3. **递归分宽度**（296 行）：每个子段拿到的是 `col_w` 而不是完整的 `width`，所以**嵌套分叉越深、盒子越窄**。这是自然的（列宽要留给并排的兄弟），但三层以上分叉会撞到 16 的下限，之后所有列都按 16 渲染、总宽开始溢出。
4. **行对齐**（299–303 行）：各列高度不同，短的用空白行补齐到最高列，然后 `zip(*padded)` 逐行拼接。这是字符网格并排的标准做法——先把每列拉成等高的字符串矩阵，再横向 concat。

**`_fork_bar`（306–313 行）** 画分叉横杆：

```python
def _fork_bar(n, col_w):
    caps = []
    for _ in range(n):
        pad = max(0, col_w // 2)
        caps.append((" " * pad) + "|" + (" " * max(0, col_w - pad - 1)))
    return "  ".join(caps)
```

每列中间一个 `|`，宽度严格等于 `col_w`，保证横杆的列位置和下方的盒子对齐。视觉上是一排 `|   |   |`，表示"下面这些列都从这里分出去"。注意它**不是** `┌┴┐` 那种连成一体的树杈——每个 `|` 是独立的一段，中间用空格隔开。选这种画法是因为分支数和列宽都不固定，画连续的 `─┬─` 需要精确计算每个分叉点的位置，而离散的 `|` 只需 `col_w // 2` 一个除法。

## 8. 字符画：`_box` / `_row` / `_label`（526–562 行）

**`_box`（526–540 行）** — 用 box-drawing 字符画一个节点：

```python
out = ["┌" + "─" * inner + "┐"]
for line in body:
    line = preview_user_text(line, inner)
    out.append("│" + line.ljust(inner)[:inner] + "│")
out.append("└" + "─" * inner + "┘")
```

`inner = width - 2`（去掉左右边框）。每一行先 `ljust(inner)` 补齐再 `[:inner]` 硬截断——**双保险**：短行靠补空格对齐右边框，超长行靠切片保证绝不超出。`preview_user_text` 再挡一道（超长加 `…`）。

body 的构成：`[label, *detail.splitlines()]` 加上工具节点的结果预览行 `"→ " + preview_user_text(result, width - 4)`。

**`_row`（542–553 行）** — 多个节点并排：

```python
col = max(18, min(36, (width - 2) // max(1, len(nodes))))
boxes = [_box(node, col, mark_path=mark_path) for node in nodes]
height = max(len(box) for box in boxes)
for box in boxes:
    box.extend([" " * col] * (height - len(box)))
for row in zip(*boxes):
    lines.append("  ".join(row))
```

列宽三重夹逼：下限 18、上限 36、均分。上限 36 是**别让单行太宽**——两个工具时均分能到 27，三个以上就撞到 18 的下限，于是总宽又一次溢出（`(18+2)*4 = 80` 已经超过默认 `width=56`）。同样用"补空白行拉齐 + `zip` 拼接"。

**`_label`（555–562 行）** — 标题行的内容：

```python
star = " *" if mark_path and node.on_path else ""
if node.kind == "tool":
    flag = f" [{node.status}]" if node.status and node.status != "ok" else ""
    return f"tool {node.title}{flag}{star}"
return f"{node.title}{star}"
```

- `*` 只在 `mark_path` 为真时出现（第 6 节解释了为什么要有这个开关）。
- 工具节点的状态标记**只在异常时显示**（`ok` 不显示 `[ok]`）——正常情况占多数，满屏 `[ok]` 是噪音，只让 `[error]`/`[blocked]` 跳出来。

## 9. HTML 渲染：`render_html` / `_html_*`（315–427、564–627 行）

**`render_html`（315–427 行）** 返回一个完整的自包含 HTML 字符串：一个 f-string，内联 `<style>`，没有任何外部依赖（不引 CDN、不引字体文件）。这个选择是有意的——`/graph html` 写出的文件要能在没网的机器、或者会话归档传走多年后单独打开。

配色是"深色 + 做旧纸"的调色板（`--steel: #14120e`、`--paper: #ead9b6`、`--brass: #c4a15a`），四类节点用左边框颜色区分：`--user` 橙、`--say` 绿、`--tool` 黄、`--bad` 红（错误/被拦截）。和 [`ui/style.py`](../ui/style.md) 的终端配色是同一套审美。

**卡片内容**（`_html_card`，583–607 行）比 ASCII 盒信息量大得多，这是 HTML 模式存在的理由：

```python
if node.kind == "tool":
    inner = _html_args(node.args)          # 结构化参数表
elif node.body.strip():
    inner = f'<div class="body">{html.escape(node.body)}</div>'   # 全文
else:
    inner = ...node.detail...
```

- 工具节点的 `args` 渲染成 `<dl class="kv">` 键值对（`_html_args`，568–581 行）。`CODE_ARG_KEYS = {"content", "old_string", "new_string", "command", "query"}`（565 行）里的参数、或者值里带换行的，进 `<pre><code>` 块——`write`/`edit` 的 `content`、`bash` 的 `command` 都是多行内容，塞进 `<dd>` 会挤成一行没法读。
- user/say 节点显示 `body` **全文**（ASCII 只能显示 120/160 字符预览），`body` 就是为此单独存在 `GraphNode` 上的字段。
- 工具结果进 `<details open>`（默认展开，可折叠）——长输出不占版面。

**转义**：所有插值都过 `html.escape`。`node.kind`/`node.title` 这类内部生成的字符串也逃，属于"无差别转义"的稳妥做法：**图上有用户输入的原文**（user 消息、工具输出），不逃会 XSS。注意 `html.escape` 默认 `quote=True`，会把 `"` 转成 `&quot;`，所以在属性值里插值是安全的。

**`_html_block`（609–627 行）** 是 `_render_block` 的 HTML 对应物，结构一一映射：

| ASCII | HTML |
|---|---|
| 层间 `"          \|"` | `<div class="edge">`（虚线渐变竖条，CSS `repeating-linear-gradient`） |
| 一层（多节点并排） | `<div class="layer">`（flex + `justify-content: center` + `flex-wrap: wrap`） |
| 多分支按列 `zip` 拼接 | `<div class="split">` 包若干 `<div class="branch">`（flex `1 1 260px`） |

**关键差别**：HTML 版**不需要算坐标**。布局交给浏览器的 flexbox，`flex-wrap: wrap` 让窄屏自动换行、`.branch` 的 `min-width: 200px` 让多分支在空间不足时纵向堆叠。这正是"大图怎么裁"在 HTML 侧的答案：**不裁，交给浏览器**。

## 10. `write_html` 与 `list_session_runs`（429–436、629–654 行）

**`write_html`（429–436 行）** 写到 `.wheel/graphs/<session_id>.html`：

```python
root = Path(workspace).resolve() / ".wheel" / "graphs"
root.mkdir(parents=True, exist_ok=True)
path = root / f"{graph.session_id}.html"
path.write_text(render_html(graph), encoding="utf-8")
```

放在 `.wheel/` 下（和 sessions、checkpoints 同级），是工作区本地产物；文件名用 session_id，`serve_graphs(path.parent)` 拿到的就是这个目录。

**`list_session_runs`（629–654 行）** 扫 `runs_dir` 下每个子目录的 `meta.json`，按 `session_id` 字段匹配，收集 `st_mtime_ns` 后排序返回 run ID 列表。它和 [`core/events.py`](../core/events.md) 里 `load_run` 的"按 session_id 查找"是**同一件事的两种实现**（那边找一个，这里列全部）——因为 `meta.json` 里记了 `session_id`，图才能顺带告诉你"这个会话跑过哪些 run，可以拿去 `/replay`"。

读写 `meta.json` 时的异常处理是静默跳过（损坏的 JSON、读不了的目录），图是展示层，不该因为一个坏文件挂掉。

## 11. HTTP 服务：`serve_graphs` / `stop_graph_server`（656–698 行）

模块级单例（`_http` / `_http_root`，657–658 行）+ 两个控制函数。

**`serve_graphs`（661–687 行）**：

```python
if _http is not None:
    if _http_root == root:
        port = int(_http.server_address[1])
        return f"http://127.0.0.1:{port}/"
    stop_graph_server()   # 换目录必须重启
```

**为什么换目录要重启**：`Handler.__init__` 用闭包捕获了 `root` 变量传给 `SimpleHTTPRequestHandler(directory=...)`。这个闭包在类定义时就绑定了**第一次**的 `root`；同一个目录复用服务能返回同一个 URL（不用重新开端口），换目录则旧服务会 404——注释明确写了这一点。

其余细节：

- `ThreadingHTTPServer(("127.0.0.1", 0), Handler)` — **只绑回环地址**（不对外暴露），端口 0 让 OS 分配空闲端口，避免端口冲突。端口从 `_http.server_address[1]` 读回。
- `threading.Thread(..., daemon=True)` 跑 `serve_forever` — 守护线程，主进程退出时不阻塞。
- `atexit.register(stop_graph_server)` 兜底关闭（每次调用都注册一次，见第 13 节）。
- 子类化 `SimpleHTTPRequestHandler` 只重写 `log_message` 为空 —— 否则每次浏览器请求都会往终端打印访问日志，污染 TUI 输出。
- `stop_graph_server`（689–698 行）做 `shutdown()` + `server_close()` 并把两个全局变量置 `None`，保证幂等（重复调用安全）。

服务的生命周期由 [`ui/app/__init__.py`](../../../ui/app/__init__.py) 在退出路径（391 行）调 `stop_graph_server` 收尾；`handle_graph` 也打印了提示 "server stops when you quit wheel"。

## 12. 与 `/tree`、`/graph` 命令的配合

先看分发（[`ui/app/__init__.py`](../../../ui/app/__init__.py) 695–697 行）：

```python
if command == "tree":
    return handle_tree(chat, rest)
if command == "graph":
    handle_graph(chat, workspace, config.runs_dir, rest)
```

**`/tree` 不走 graph.py**（113–150 行 @ commands.py）。它的数据源是 `Session.tree_rows()`（[`core/session.py`](../../../core/session.py)）：**每个 user 消息一行**，带 `depth`（沿途 user 祖先的数量）、`on_path`、`leaf`、`label`（前 80 字符）。渲染成 `* ` + `  `×depth + id + label 的缩进列表。

**交互（选择/高亮/翻页）全在 `pick_list`**（[`ui/repl.py`](../../../ui/repl.py) 827 行），不在本模块：

- 高亮：`mark = ">" if i == selected`，选中行 `style.cyan`，其余 `style.dim`。
- 选择：↑↓ 选（循环），回车确认，Esc/`q`/Ctrl+C 取消（返回 `None`）。
- 翻页：**没有翻页**。`paint()` 每次重画全部 `n` 行（先 `\033[{n}A` 光标上移 `n` 行再逐行覆盖）。选项超过一屏时会画到屏幕外——`pick_list` 不做窗口滚动。
- 非 tty 环境（管道/`--json`）直接返回 `selected` 默认值，不进交互。

`handle_tree` 的默认选中项是 `rows` 里 `leaf` 为真的那行（当前对话位置），选中后调 `session.fork(spec)` —— **跳转就是移动 leaf 指针**，零拷贝分叉（见 [session.py 文档](../core/session.md)）。

**`/graph` 走 graph.py**（153–170 行 @ commands.py）：

```python
graph = build_session_graph(session, runs_dir)
if not graph.layers and graph.tree.empty():
    print(style.dim("(empty session)"))
    return
if rest.strip().lower() in {"html", "open", "web", "serve"}:
    path = write_html(graph, workspace)
    url = serve_graphs(path.parent)
    print(style.green(f"html  {path}"))
    print(style.green(f"http  {url}{path.name}"))
    return
print(render_ascii(graph), end="")
```

- 空会话判定用 `graph.layers`（线性视图）**和** `graph.tree`（完整结构）两者都空 —— 双保险，因为两者在建图时是独立得出的。
- 子命令 `html`/`open`/`web`/`serve` 四个别名都触发 HTML 模式（降低记忆负担）；输出打印文件路径和可直接点开的 `http://127.0.0.1:<port>/<session_id>.html`。
- **`/graph` 完全不可交互**：没有选择、没有高亮、没有翻页，`print` 完就结束（这也是为什么 `render_ascii` 不做终端宽度自适应的副作用有限——图是"打印出来往回翻"，不是"占住屏幕操作"）。

两个命令共享的只有一件事：**entry ID**。`/tree` 跳转用的 ID、`/graph` header 里的 `now <leaf_id>`、以及图上 user 节点的编号 `user N`，都指向同一套 `Session.entries` 的键，可以互相参照。
