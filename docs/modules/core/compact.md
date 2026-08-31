# `core/compact.py` 逐段讲解

> 本篇讲上下文紧凑（compaction）。上游是 [loop.py](../../loop-explained.md) 的主循环和 `/compact` 斜杠命令，下游是 [context.py](./context.md) 的 token 估算和 [model.py](../../../core/model.py) 的一次摘要调用。

把超窗的对话历史压成一条结构化摘要：切点之前的旧前缀换成摘要消息，切点之后的条目逐字节不动——既压下去，又保住 provider 的前缀缓存。

- 行数：306 行
- 依赖：
  - [context.py](./context.md) —— `estimate_item_tokens` / `estimate_items_tokens`（1 token ≈ 4 字符的粗估算）、`tag_lines`（从旧摘要里抓 `<read-files>` 标签）
  - [model.py](../../../core/model.py) —— `ModelClient.complete()`（摘要本身由一次模型调用生成）、`extract_text` / `item_text`
  - [types.py](./types.md) —— `Item`、`Usage`、`unique`（保序去重）
- 被谁用：
  - [loop.py](../../loop-explained.md) —— 收尾顺手紧凑（292–318 行）和上下文溢出自愈（416–434 行），见该文第 13、17 节
  - [session.py](./session.md) —— `apply_compact()` 把紧凑结果落成叠加层，并自增 `cache_epoch`
  - [ui/app/commands.py](../ui/app-commands.md) —— `/compact` 命令强制压缩
  - [ui/graph.py](../ui/graph.md)、[ui/app/live.py](../ui/app-live.md)、[session.py](./session.md) —— `is_summary_item()` 用来把摘要消息和真 user 回合区分开

## 目录

- [1. 导入（1–12 行）](#1-导入112-行)
- [2. 三个常量：余量、保留量、摘要标记（15–20 行）](#2-三个常量余量保留量摘要标记1520-行)
- [3. `CompactStats`：一次紧凑的统计（24–40 行）](#3-compactstats一次紧凑的统计2440-行)
- [4. 摘要 prompt 常量（42–63 行）](#4-摘要-prompt-常量4263-行)
- [5. `should_compact()`：触发判定（66–70 行）](#5-should_compact触发判定6670-行)
- [6. `is_context_overflow()`：认出超窗报错（73–85 行）](#6-is_context_overflow认出超窗报错7385-行)
- [7. `is_summary_item()`：识别已有摘要（88–92 行）](#7-is_summary_item识别已有摘要8892-行)
- [8. `_starts_valid_suffix()`：API 合法的后缀起点（95–100 行）](#8-_starts_valid_suffixapi-合法的后缀起点95100-行)
- [9. `find_user_cut_index()`：算切点（103–137 行）](#9-find_user_cut_index算切点103137-行)
- [10. `previous_summary()`：取旧摘要（140–145 行）](#10-previous_summary取旧摘要140145-行)
- [11. `collect_file_ops()`：收集文件清单（148–176 行）](#11-collect_file_ops收集文件清单148176-行)
- [12. `compact_items()`：执行一次紧凑（179–210 行）](#12-compact_items执行一次紧凑179210-行)
- [13. `compact_history()`：循环里的入口（213–246 行）](#13-compact_history循环里的入口213246-行)
- [14. `serialize_items()`：历史序列化成文本（249–269 行）](#14-serialize_items历史序列化成文本249269-行)
- [15. `_summarize()`：调模型生成摘要（272–288 行）](#15-_summarize调模型生成摘要272288-行)
- [16. `_ensure_file_tags()`：补齐文件标签（291–298 行）](#16-_ensure_file_tags补齐文件标签291298-行)
- [17. `_item_text()`：取文本（301–306 行）](#17-_item_text取文本301306-行)

---

## 1. 导入（1–12 行）

三样依赖，各对应紧凑要干的一件事：**估 token**（context）、**调用模型写摘要**（model）、**消息结构与保序去重**（types）。注意导入的是 `Item = dict[str, Any]` 这个宽松 dict 类型——紧凑要在消息里增删字段，dict 比 dataclass 好改。

## 2. 三个常量：余量、保留量、摘要标记（15–20 行）

```python
RESERVE_TOKENS = 16_384      # 触发前留给输出的 token 余量
KEEP_RECENT_TOKENS = 20_000  # 保留最近多少 token 原文不动
SUMMARY_MARK = "[SESSION SUMMARY — not a user message]"
```

三个数决定了整篇的行为，逐个说选取理由：

- **`RESERVE_TOKENS = 16_384`**：紧凑**触发**的阈值是"输入 token > 窗口 − 预留"。留 16k 是因为触发判定发生在**发请求之前**，下一次请求还要带上工具 schema、系统提示和模型输出；不留余量就会卡在窗口边缘反复触发。作者设定值，量级上是常见输出上限（8k/16k）。
- **`KEEP_RECENT_TOKENS = 20_000`**：紧凑**保留**多少原文。这是"摘要覆盖率"和"缓存有效性"的折中——保留越多，摘要要概括的内容越少（损失小），但压完的总量越大；20k 大约是 128k 窗口的 15%。
- **`SUMMARY_MARK`**：摘要消息开头的一行标记，见第 7 节讲它为什么存在。用的是 U+2014 em dash，不是普通连字符——手工拼同样字符串时容易踩。

## 3. `CompactStats`：一次紧凑的统计（24–40 行）

五个字段，`did` + 前后各两项：

| 字段 | 含义 |
|---|---|
| `did` | 是否真的压了（`False` = no-op，前缀没动） |
| `before_items` / `after_items` | 压缩前后消息条数 |
| `before_tokens` / `after_tokens` | 压缩前后估算 token 总数 |

`as_dict()` 手写字段而非用 `asdict()`（同文件 `Usage` 就用了 `asdict`），因为事件流 `compact` 事件的字段要稳定，不想以后加私有字段时漏进 JSON。

统计的去向：`session.last_compact` 存一份（[session.py](./session.md)），`bus.emit("compact", ...)` 发一份（[loop.py](../../loop-explained.md) 第 13 节），UI 在 `/compact` 里把 `before → after` 打成一行给人看。

## 4. 摘要 prompt 常量（42–63 行）

`SUMMARIZE_INSTRUCTIONS`（42–46 行）是给摘要模型的 system 指令，只一句"只回摘要正文，不要开场白"——摘要要被拼进历史喂回模型，任何"Here is a summary:" 之类的套话都是纯浪费 token。

`SUMMARIZE_PROMPT`（50–63 行）硬约定了七个标题：

```
## Goal / ## Constraints & Preferences / ## Progress
### Done / ### In Progress / ### Blocked
## Key Decisions / ## Next Steps / ## Critical Context
```

这份结构是**多轮紧凑不丢信息的机制**：下一次紧凑时旧摘要会被原文喂回来（第 10、15 节），模型按同样的标题更新，于是"三段式进度 + 关键决策 + 下一步"这个骨架跨多少次压缩都在。标题里特意留了 `### In Progress` 和 `### Blocked`——coding agent 的续接最怕丢"做到哪、卡在哪"。

## 5. `should_compact()`：触发判定（66–70 行）

```python
def should_compact(input_tokens, context_window, reserve=RESERVE_TOKENS) -> bool:
    if context_window <= 0:
        return False
    return input_tokens > context_window - reserve
```

两点：

- `context_window <= 0` 直接返回 `False`——窗口未知（配置没填、模型名查表没查到）时**宁可不压**，而不是按 0 算出"一定超窗"然后疯狂压缩。
- `input_tokens` 是**provider 上报的真实用量**，不是估算。README 第 3 条特别强调这点：估算（1 token ≈ 4 字符）只用来算切点和统计，触发看真实值，否则会在计费口径上差出一大截。这个值由 [loop.py](../../loop-explained.md) 传进来：收尾紧凑用 `last_usage.input_tokens`，溢出重试用 `context_window` 本身（已经超了，直接顶格）。

## 6. `is_context_overflow()`：认出超窗报错（73–85 行）

```python
needles = ("context_length_exceeded", "context length", "maximum context",
           "prompt is too long", "too many tokens", "token limit", "context window")
return any(needle in text for needle in needles)
```

`compact_history` 只在**被触发**时压，而超窗是 provider 抛异常告知的——各家措辞不同（OpenAI 用 `context_length_exceeded`，Anthropic 系爱说 `prompt is too long`，还有些网关只说 `token limit`），这里用一串小写子串匹配兜住常见说法。

用子串而非精确匹配，代价是**误判风险**：一条恰好含 "token limit" 的无关报错也会被当成超窗，于是走一遍紧凑再重试一次。这是有意的取舍——紧凑重试的代价是一次额外的模型调用，比直接放弃整个 run 便宜。

调用点在 [loop.py](../../loop-explained.md) 的 `_complete_with_overflow`（416 行）：`if not compact or not is_context_overflow(exc): raise`——不是超窗的错原样上抛，超窗才压完重试一次。

## 7. `is_summary_item()`：识别已有摘要（88–92 行）

```python
def is_summary_item(item: Item) -> bool:
    if item.get("role") != "user":
        return False
    return SUMMARY_MARK in _item_text(item)
```

摘要消息伪装成 `role: "user"`，却需要被识别出来。为什么非要装成 user 消息：

1. **API 合法**：OpenAI 的 item 序列里，一条没有配套 `function_call` 的 `function_call_output` 会被拒；而 user 消息永远可以安全地作为历史的第一条/前几条。
2. **模型理解**：把摘要放在 user 位，模型会把它当"用户提供的背景"而不是自己说过的话。

代价就是它会被所有"数 user 回合"的逻辑误算，所以 `SUMMARY_MARK` 成了一个跨模块的去重信号，四处都在用它排除：

- [session.py](./session.md) `user_turns()`（507 行）和 `tree_rows()`（350 行）、`first_user_preview_from_path()`（579 行）——不把摘要算成用户回合、不拿它做会话预览
- [ui/graph.py](../ui/graph.md)（440 行）—— 画会话树时区分真 user 节点
- [ui/app/live.py](../ui/app-live.md)（410 行）—— 渲染时给摘要加标记而不是当成用户输入

## 8. `_starts_valid_suffix()`：API 合法的后缀起点（95–100 行）

```python
return item.get("role") in {"user", "assistant"} or item.get("type") == "function_call"
```

保留后缀的**第一条**必须能合法开头。`function_call_output` 不能作为第一条（没有配对的 call，API 会拒），所以合法起点只有：user 消息、assistant 消息、`function_call`。

注意 `function_call` 也算合法起点：允许后缀从"模型刚发起一个调用、结果还没回"的位置开始——这种半截状态在 steer 注入、abort 之后的上下文里会出现。

## 9. `find_user_cut_index()`：算切点（103–137 行）

整个模块最核心的函数，四步：

**第 1 步，先判能不能压**（109–114 行）：收集所有 user 消息的索引，没有 user 消息返回 `None`；估算总量不超过 `keep_recent_tokens` 也返回 `None`——**小会话紧凑是 no-op**，这正是 README 第 5 条说的行为，也让 `cache_epoch` 不需要动。

**第 2 步，从尾部倒着累计**（115–122 行）：从最后一条往前累加估算 token，累计到 `keep_recent_tokens` 时记下位置 `threshold`。倒着走是因为"保留最近"的语义按 token 量算，而消息长度差异极大（一条工具输出可能顶一百条短消息）。

**第 3 步，对齐到 user 边界**（123–127 行）：

```python
for idx in user_indices:
    if idx >= threshold and idx > 0:
        return idx
```

返回第一个"不早于 threshold"的 user 索引。`idx > 0` 保证**前缀非空**（切在 0 等于没压）。只在 user 轮边界切有两个理由：

- 摘要的契约干净：**"该 user 轮之前的全部"**——模型写摘要时面对的是一个完整的任务段，不会概括到一半的对话。
- 保留后缀是完整回合，不会出现孤儿的 `function_call_output`。

**第 4 步，长单任务的退路**（128–136 行）：

```python
cut = user_indices[-1]
if cut > 0:
    return cut
for i in range(max(1, threshold), len(items)):
    if _starts_valid_suffix(items[i]):
        return i
return None
```

兜底链是：理想切点找不到 → 用最后一个 user 索引 → 还不行就从 `threshold` 往后找第一个 API 合法边界（第 8 节）。

这个退路是为**长单任务**准备的：上下文里只有第一条 user（任务本身）而在阈值之前，没有任何中间 user 边界可切。此时如果直接返回 `None`，历史会一路涨到溢出、无从压缩；宁可牺牲"user 边界"这个理想性质，也要让压缩能进行下去。注释里写得很直白："让运行还能压下去，而不是一路涨到溢出"。

## 10. `previous_summary()`：取旧摘要（140–145 行）

遍历历史找第一条 `is_summary_item` 为真的消息，返回其正文。多轮紧凑时它就是"待更新摘要"，交给第 15 节拼进 prompt。

只取**第一条**：紧凑后的历史里摘要永远在位置 0，再压一次时新摘要还是落在 0，所以第一条就是最新的那份。

## 11. `collect_file_ops()`：收集文件清单（148–176 行）

返回 `(read_files, modified_files)`，三处细节：

1. **先看旧摘要**（155–159 行）：用 `tag_lines(prior, "read-files")` / `"modified-files"` 从旧摘要的标签块里把上一轮记录的文件路径捞回来。不这么做的话，第二次紧凑会把"更早期读/改过哪些文件"彻底丢掉——那些消息已经不在历史里了。这是**摘要链**（第 4 节结构的配套）里信息回流的一环。
2. **扫历史里的 `function_call`**（160–175 行）：`read` 的 `path` 进 read 列表，`write` / `edit` 的进 modified 列表。`arguments` 可能是 dict 也可能是 JSON 字符串（取决于模型怎么回、以及 [session.py](./session.md) 落盘后再读出时的形态），两种都处理；解析失败就当空 dict 跳过。
3. **`unique()` 保序去重**（176 行）：路径重复出现很常见（同一个文件读改多次），去重后保持**首次出现顺序**——模型看到的文件顺序大致是真实操作顺序，比排序后的字典序有用。

## 12. `compact_items()`：执行一次紧凑（179–210 行）

真正的压缩动作：

```python
cut = find_user_cut_index(items, keep_recent_tokens)
if cut is None:
    return items, usage        # 原对象返回，调用方用 is 判 no-op
```

**返回 `items` 本身（同一个对象）而不是新列表**，是给上层判 no-op 用的——见第 13 节 `stats.did = compacted is not items`。

**保留部分仍超窗时逐倍缩小**（189–197 行）：

```python
if context_window > 0:
    kept_est = estimate_items_tokens(items[cut:])
    shrinking = keep_recent_tokens
    while kept_est > context_window - RESERVE_TOKENS and shrinking > 2_000:
        shrinking = shrinking // 2
        nxt = find_user_cut_index(items, shrinking)
        if nxt is None or nxt <= cut:
            break
        cut = nxt
        kept_est = estimate_items_tokens(items[cut:])
```

按 `KEEP_RECENT_TOKENS` 切完之后，保留段自己可能就已经超窗（比如单条工具输出就有 100k）。这时把 `keep_recent_tokens` 减半重切，直到保留段塞得进窗口，或降到 2_000 这个地板值停手。`nxt <= cut` 时 break 是防止切点回退（更小的 keep 值反而切出更前的点，逻辑上不该发生，但边界情况保个险）。

**拼接**（198–210 行）：

```python
prefix, kept = items[:cut], items[cut:]
if not prefix:
    return items, usage          # 前缀为空：没东西可压
summary, usage = _summarize(prefix, model, items)
summary_item: Item = {"role": "user", "content": f"{SUMMARY_MARK}\n\n{summary}"}
return [summary_item, *kept], usage
```

关键点：`kept` 是原列表的切片，**元素对象逐字节不变**。这正是 README 前缀缓存策略第 1 条"已发送的历史绝不改写"的落地点——provider 已缓存的那部分前缀保持原样，缓存继续命中；变的只有它之前的部分被换成了一条摘要。

注意 `_summarize` 传的是 `items`（全历史）而不是 `prefix`：文件清单要从全历史收集，已改过的文件路径不能因为消息被压掉就丢。

## 13. `compact_history()`：循环里的入口（213–246 行）

上层（[loop.py](../../loop-explained.md)、`/compact` 命令）唯一调用的公开函数：

```python
del plan_text   # 兼容参数：计划状态现在存 Session.plan，不在历史里
before_tokens = estimate_items_tokens(items)
stats = CompactStats(before_items=len(items), after_items=len(items),
                     before_tokens=before_tokens, after_tokens=before_tokens)
compacted = items
if force or should_compact(input_tokens, context_window):
    compacted, extra = compact_items(compacted, model, workspace, context_window=context_window)
    usage.add(extra)
    stats.did = compacted is not items
    stats.after_items = len(compacted)
    stats.after_tokens = estimate_items_tokens(compacted) if stats.did else before_tokens
return compacted, usage, stats
```

- `force=True` 时跳过 `should_compact` 判定——两条路径都靠它：溢出重试（已经报错了，别再判）、`/compact` 手动命令（用户就是要压）。
- `stats` 初始化成 "before == after"，`after_tokens` 只在 `did` 时重算，所以 no-op 的统计天然是零变化。
- **`workspace` 参数只往下传，本函数不用**；再往下 `compact_items` 的 `workspace` 参数**完全没被使用**（可用 AST 验证：函数体里没有任何 `workspace` 引用）。这是历史遗留的签名，留着是为了不改调用方。

调用方拿到 `(items, usage, stats)` 之后必须做三件事，缺一件就会出错（[loop.py](../../loop-explained.md) 295–315 行是标准写法）：

```python
if compacted is not items:
    items[:] = compacted          # ① 原地替换，保持列表引用不变
if session is not None:
    session.apply_compact(items)  # ② 落叠加层 + 自增 cache_epoch
    if stats.did: session.compactions += 1
```

**为什么 `items[:] = compacted` 而不是 `items = compacted`**：`run_agent` 里 `items` 是 `session.items` 的别名，重新赋值会断开这个别名，session 就看不到压缩结果了。

**为什么 `cache_epoch` 必须变**：provider 的缓存按 `prompt_cache_key` 分区（见 [session.py](./session.md) 的 `cache_key` 属性，形如 `session_id:epoch`）。普通回合只是追加消息，前缀没变、键没变，缓存持续命中；紧凑**改写了前缀**，旧分区的缓存必然失效，于是 `apply_compact()` 自增 epoch，provider 从新键开始重新积累缓存，旧分区不会被错误复用。这是 README 前缀缓存策略第 2 条。

## 14. `serialize_items()`：历史序列化成文本（249–269 行）

把 `prefix` 渲染成摘要模型能读的纯文本，每种消息一个前缀标签：`[User]` / `[Assistant]` / `[Assistant tool call]` / `[Tool result]` / 兜底的 `[kind]` + JSON。

两处截断值得注意：

- **工具输出超 4000 字符截断**（261–263 行），加 `…` 结尾。工具输出是历史里体积最大的部分（一次 `grep` 几千行），但摘要只需要知道"这个调用返回了什么性质的结果"。
- **兜底分支 JSON 截断 2000 字符**（268 行）：未知 item 类型（各 provider 的思考块、自定义 item）整个 dump 出来，限长防止单条未知 item 把 prompt 撑爆。

## 15. `_summarize()`：调模型生成摘要（272–288 行）

```python
prior = previous_summary(prefix)
read_files, modified_files = collect_file_ops(all_items)
prompt = SUMMARIZE_PROMPT
if prior:
    prompt += "\nPrevious summary to update:\n" + prior + "\n"
prompt += "\nHistory:\n" + serialize_items(prefix)
response = model.complete([{"role": "user", "content": prompt}],
                          tools=[], instructions=SUMMARIZE_INSTRUCTIONS)
text = extract_text(response.output).strip() or serialize_items(prefix)[:2000]
text = _ensure_file_tags(text, read_files, modified_files)
```

- **用的哪个 model**：就是主循环传进来的那个 `ModelClient`——同 provider、同模型、同推理档（[loop.py](../../loop-explained.md) 把 `model` 一路传进来）。不单独配一个"廉价摘要模型"，省一层配置，代价是摘要调用按主模型计费。
- **`tools=[]`**：摘要调用不需要任何工具，传空 schema 省 token 也避免模型自作主张调工具。
- **失败退路**（286 行）：`extract_text` 拿不到文本（模型回了空、或只回了思考块）时，退回"截断到 2000 字符的原文"。**有损但不丢整段**——比抛异常让整个紧凑失败好。README 说"摘要天然有损"，这是有损的底在哪。
- **文件清单强制补齐**（288 行）：先靠模型写（prompt 里要求保留 file paths），再靠代码兜底，见下一节。

返回 `response.usage`，由调用方 `usage.add(extra)` 累加——**摘要调用的 token 也要计费**，不能白算。

## 16. `_ensure_file_tags()`：补齐文件标签（291–298 行）

模型写摘要可能漏掉文件清单，所以代码强制补：

```python
if read_files and "<read-files>" not in body:
    body += "\n\n<read-files>\n" + "\n".join(read_files) + "\n</read-files>"
```

先检查标签是否已存在（模型写了就不重复加），没有才追加。这套标签和第 11 节的 `tag_lines` 是**一对**：写入用 `<tag>` 块，读出用 `tag_lines` 解析——摘要链靠这两个函数把文件上下文一代代传下去。

## 17. `_item_text()`：取文本（301–306 行）

```python
text = item_text(item)
if not text and item.get("output"):
    return str(item["output"])
```

`model.item_text()` 只认 `content` 字段，而 `function_call_output` 的正文在 `output` 字段里。这个函数补上回退，让 `is_summary_item` / `previous_summary` / `serialize_items` 对工具输出也不会拿到空串。

---
