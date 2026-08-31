# `core/plan.py` 逐段讲解

> 本篇讲计划（plan）机制的状态与确认流。上游是 [tools/tools.md](../tools/tools.md) 里的 `plan` 工具与 [core/prompt.md](../core/prompt.md) 的 ephemeral 注入，下游是 [core/loop.md](../../loop-explained.md) 的 `plan_rejected` 停机分支。

`PlanStore` 是"模型先交计划、用户 y/N 批准、再动文件"这条约束的状态机：存步骤列表、记住批准/拒绝标记、渲染成文本喂回给模型和 UI。

- 行数：124 行
- 依赖：标准库（`collections.abc`、`typing`）——**零内部依赖**，这是全项目最独立的模块之一
- 被谁用：
  - [tools/tools.md](../tools/tools.md) —— `ToolRuntime._plan()` 调用 `replace()`，`_rejected_plan_block()` 读 `confirmed` / `rejected` 拦截 write/edit
  - [core/prompt.md](../core/prompt.md) —— `ephemeral_items()` 每轮把 `render()` 结果和批准状态注入系统消息
  - ../../loop-explained.md —— 循环持有 `PlanStore`、识别 `plan_rejected`、紧凑时传 `plan_text`
  - [core/session.md](../core/session.md) —— `Session.plan` 字段，跨轮持久化的计划状态
  - [ui/app-live.md](../ui/app-live.md) —— `footer_lines()` 渲染终端页脚

## 目录

- [1. 常量与异常（1–22 行）](#1-常量与异常122-行)
- [2. `PlanStore.__init__`（24–33 行）](#2-planstore__init__2433-行)
- [3. `replace()`：替换计划与确认流（35–71 行）](#3-replace替换计划与确认流3571-行)
- [4. `render()` 与 `footer_lines()`（73–87 行）](#4-render-与-footer_lines7387-行)
- [5. `normalize_steps()`：步骤校验（89–115 行）](#5-normalize_steps步骤校验89115-行)
- [6. `format_plan()`：复选框渲染（118–124 行）](#6-format_plan复选框渲染118124-行)

---

## 1. 常量与异常（1–22 行）

```python
STATUSES = ("pending", "in_progress", "done")
```

三种状态。`completed` 是模型常写出来的别名，在 `normalize_steps()` 里归一成 `done`（94 行）——**在入口宽松、内部统一**，避免状态字符串在下游散开。

`PlanRejected(Exception)`（12–14 行）：用户拒绝时抛。它不是错误，是**控制流信号**。`ToolRuntime._plan()` 把它转成 `ValueError`，于是工具执行器把它当普通工具异常捕获，返回一个 `is_error=True` 的 `ToolResult`，文本就是 `REJECTED_HINT`。循环再从结果文本里认出 "rejected"（见第 7 节）。

`REJECTED_HINT`（16–21 行）的措辞是给模型看的指令，不是给人看的提示：

> plan rejected by user — do not implement. On the next user message, call the plan tool again with a revised step list that incorporates their feedback. The harness will ask y/N again.

三句话分别堵三个漏洞：别直接开写、要带着反馈改计划、改完还会再问一次（防止模型以为被拒就永远不能提交）。这段文本同时是循环的识别依据——循环靠 `"rejected" in item.output.lower()` 判断，所以**它必须留在文本里**，改措辞时不能删掉 "rejected" 这个词。

## 2. `PlanStore.__init__`（24–33 行）

```python
self.steps: list[dict[str, str]] = []
self.confirmed = False
self.rejected = False
self.ask = ask
self.interactive = interactive
```

状态只有四个字段，但 `confirmed` / `rejected` **不是冗余的布尔对**，是三位状态：

| `confirmed` | `rejected` | 含义 | ephemeral 里注入的话 |
|---|---|---|---|
| `False` | `False` | 还没提交过 / 计划待确认 | "Submit it with the plan tool so the harness can ask the user." |
| `True` | `False` | 已批准，可以干活 | "Plan is approved. Continue pending/in_progress steps now." |
| `False` | `True` | 上次提交被拒 | "Call the plan tool with a revised step list. Do not write or edit files until..." |

（`confirmed=True, rejected=True` 这个组合在代码里不会出现：`replace()` 批准后同时把 `rejected` 置回 `False`。）

`ask` 和 `interactive` 两个参数决定"会不会弹窗"：

- `ask`：y/N 询问回调，由 UI 传入（`ToolRuntime` 里是 `PlanStore(ask=safety.ask, interactive=safety.interactive)`，两者都从 [SafetyGate](../tools/safety.md) 继承——安全门和计划确认共用同一个提问通道和交互标志）。
- `interactive`：`False` 时（`--json` 一次性模式、测试）**跳过询问直接批准**（48 行的 `if self.interactive and self.ask` 不满足，直接落到 53 行 `self.confirmed = True`）。非交互场景没有人在另一端回答，自动批准是唯一合理行为。

## 3. `replace()`：替换计划与确认流（35–71 行）

模型每次调用 `plan` 工具都发**完整**步骤列表（不是增量 diff），所以方法名叫 `replace`。

**第一步：归一化 + 取内容指纹**（37–40 行）

```python
steps = normalize_steps(raw)
new_contents = [step["content"] for step in steps]
old_contents = [step["content"] for step in self.steps]
old_done = {step["content"] for step in self.steps if step["status"] == "done"}
```

比较用 `content` 而非整条 dict——**步骤内容变了才重新问**，改个状态不算变。

**第二步：判断是否要确认**（42 行）

```python
needs_confirm = (not self.confirmed) or new_contents != old_contents
```

两条触发路径：首次提交（还没批准过）、或步骤内容变了。反之，**已批准且内容没变 = 纯进度更新，免二次确认**（58 行之后）。这个设计让"标记第 3 步 done"这种每步一次的调用不会反复弹窗——否则一个 10 步计划要按 10 次 y。

**第三步：需要确认时问用户**（43–51 行）

```python
if self.interactive and self.ask:
    prompt = "Proposed plan:\n" + format_plan(steps) + "\nProceed with this plan?"
    if not self.ask(prompt):
        if not self.confirmed:      # 只在从未批准过时记 rejected
            self.steps = steps
            self.rejected = True
        raise PlanRejected(REJECTED_HINT)
```

`if not self.confirmed` 这一层判断是关键的取舍：**已经批准过的计划里改步骤被拒，不置 `rejected`**（49–51 行）。因为此时任务早就在进行中，用户否掉的只是"新增/修改的步骤"，不该把整个任务卡死、连已经批准的部分也不让写。而 `self.steps = steps` 放在 `if` 里面，意味着这种情况下**被拒的新步骤不会被采纳**，`steps` 保持用户已批准的那份。

抛 `PlanRejected` 前先把被拒的计划存进 `self.steps`，这样 ephemeral 注入时模型能看见"我刚才提的是这个、被拒了"（配合 `rejected=True` 的提示语），改起来有参照。

**第四步：批准**（52–58 行）

```python
self.confirmed = True
self.rejected = False
self.steps = steps
return "plan approved — continue with these steps now. Do not ask for another confirmation in chat.\n" + format_plan(steps)
```

"Do not ask for another confirmation in chat" 这句是防模型在对话里再问一遍"这样可以吗？"——确认已经在弹窗里做过了，再问一遍就是死循环式的废话。

**第五步：免确认的进度更新**（59–71 行）

```python
self.steps = steps
jumped = len({step["content"] for step in steps if step["status"] == "done"} - old_done)
extra = ""
if jumped > 1:
    extra = "\nnote: mark one newly finished step per plan call so progress stays visible."
return "plan updated\n" + format_plan(steps) + extra
```

`jumped` 是这次调用**新增**的 done 步骤数（集合差，只数新建的）。一次标完多个 `done` 说明模型在补记进度——用户的页脚看到的是"还差 3 步"直接跳到"全完成"，中间过程不可见。所以附一句引导提示（70 行）：一次只标一个。这是**软提示，不是报错**——不打断执行。

## 4. `render()` 与 `footer_lines()`（73–87 行）

`render()`（73–75 行）：完整计划文本，空计划时返回 `"(empty plan)"`。它有两个去处：

- [core/prompt.md](../core/prompt.md) 的 `ephemeral_items()` —— 每轮包进 `<plan>` 标签注入系统消息，**不进历史**（所以每轮都是最新状态，且不破坏前缀缓存）；
- ../../loop-explained.md 的收尾紧凑（301 行）—— 作为 `plan_text` 参数传给 `compact_history()`，保证摘要里不丢当前计划。

`footer_lines(busy=False, max_lines=10)`（77–87 行）：终端页脚用的裁剪版，两条隐藏/裁剪规则：

```python
if (not busy) and (not self.rejected) and all(step["status"] == "done" for step in self.steps):
    return []
```

**全部完成且空闲且不处于被拒状态 → 返回空列表，页脚不显示计划**。跑完了还占着屏幕是噪音。忙碌中（`busy=True`）不隐藏——用户正盯着进度看。`self.rejected` 也不隐藏——被拒的计划必须保持可见，否则用户看不见"为什么现在不能写文件"。

超长时截到 `max_lines` 行，末尾补 `"…"`（85–86 行）：`lines[: max_lines - 1] + ["…"]`，注意是 `max_lines - 1` 行正文 + 省略号，总共仍占 `max_lines` 行。

## 5. `normalize_steps()`：步骤校验（89–115 行）

纯函数，把模型发来的任意 JSON 收成规范步骤列表。六道校验，全部抛 `ValueError`（由工具执行器捕获成 `is_error=True` 的结果回给模型，模型自己重试）：

1. **非空**（90–91 行）：`not isinstance(raw, list) or not raw` → "plan requires a non-empty steps array"。空计划没有意义——真要清空应该走别的方式。
2. **每条是对象**（95–96 行）：模型偶尔会发字符串数组。
3. **content 非空**（100–101 行）：空步骤无法在 UI 上区分，也没法做去重键。
4. **状态合法**（102–103 行）：不在 `STATUSES` 里就报错，错误消息带上实际值（`"got {status!r}"`）——模型看到自己的错误输入才改得快。
5. **content 去重**（104–106 行）：重复步骤让"标记 done"产生歧义（标哪个？）。`content` 被当作步骤的**稳定身份**，第 3 节的 `old_done` 集合差也靠它。
6. **最多一个 `in_progress`**（113–114 行）：这是计划机制的核心约束——一次只做一步。多步并行时"当前在做什么"失去意义，页脚和模型自己都会乱。

## 6. `format_plan()`：复选框渲染（118–124 行）

```python
marks = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}
```

渲染成编号复选框清单：

```
1. [x] 读 loop.py
2. [>] 写文档
3. [ ] 跑测试
```

`marks.get(step['status'], '[ ]')` 有兜底：万一状态不在表里（理论上 `normalize_steps` 已拦），不当成异常，退化成 `[ ]`。这份文本同时给模型看、给用户看、进紧凑摘要——**一份渲染三处使用**，改样式时三处同步变。

---
