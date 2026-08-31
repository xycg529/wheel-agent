# `harness/refine.py` 逐段讲解

> 本篇讲 `/refine` 的提炼流程：把一段对话轨迹交给模型，产出对 harness 的结构化修改提案并应用。上游是 `ui/app/refine.py`（TUI 的手工/自动两条入口），下游是 `harness/harness.py`（存储层）和 `core/model.py`（模型调用）。

一句话职责：**从会话轨迹里提炼可复用经验，变成对 harness 笔记库的批量 edits，带 CAS 并发检查地应用进去，并支持按 id 回滚**。

- 行数：359 行
- 依赖：
  - [`harness/harness.md`](harness.md) —— 存储层：`apply_proposal` / `rollback_proposal` / `snapshot_state` / `HarnessStore`
  - [`core/model.md`](../core/model.md) —— `ModelClient`、`extract_text`、`make_client`
  - [`core/compact.md`](../core/compact.md) —— `serialize_items`（把会话拍成文本）
  - [`core/types.md`](../core/types.md) —— `Item`、`Usage`
- 被谁用：
  - [`ui/app-refine.md`](../ui/app-refine.md) —— 唯一构造 refine 调用的地方（`_execute_refine`）
  - [`ui/app-commands.md`](../ui/app-commands.md) —— `/harness`、`/refine` 相关命令分发

## 目录

- [1. 模块定位与两条工作模式](#1-模块定位与两条工作模式1-24-行)
- [2. `REFINEMENT_INSTRUCTIONS`：提炼模型的 system 指令](#2-refinement_instructions提炼模型的-system-指令2657-行)
- [3. 两个常量：`CONVERSATION_CHARS` 与 `TRUNCATED_JSON`](#3-两个常量conversation_chars-与-truncated_json5864-行)
- [4. 自动 refine 的节奏控制](#4-自动-refine-的节奏控制6684-行)
- [5. `parse_refine_args`：命令参数解析](#5-parse_refine_args命令参数解析86105-行)
- [6. `extract_json_object`：从回复里捞 JSON](#6-extract_json_object从回复里捞-json107130-行)
- [7. `normalize_proposal`：提案归一化](#7-normalize_proposal提案归一化132142-行)
- [8. `plan_refinement`：生成提案](#8-plan_refinement生成提案144189-行)
- [9. `run_refine`：端到端执行](#9-run_refine端到端执行191236-行)
- [10. `format_refine_result` 与 `_edit_text`：结果渲染](#10-format_refine_result-与-_edit_text结果渲染238269-行)
- [11. `_complete_json`：关推理档的模型调用](#11-_complete_json关推理档的模型调用271292-行)
- [12. `_parse_json` 与 `_incomplete`：区分截断和格式错](#12-_parse_json-与-_incomplete区分截断和格式错294329-行)
- [13. `_overview` 与 `_history_for_prompt`：给模型看的上下文](#13-_overview-与-_history_for_prompt给模型看的上下文331359-行)

---

## 1. 模块定位与两条工作模式（1–24 行）

模块 docstring 点明四个能力：**调模型产出 JSON 提案 → 应用到 local/global 存储 → 支持回滚 → 解析 `/refine` 命令参数**。

导入分两拨，界线很清楚：

- 从 `harness.harness` 导入的是**存储层的全部写路径原语**：`apply_proposal`（应用提案）、`rollback_proposal`（由历史记录反推回滚提案）、`snapshot_state`（快照做 CAS 基线）、`generate_refinement_id`（生成 id）、`load_history`（读历史）、`HarnessState` / `HarnessStore`。
- 从 `core` 导入的只有三样：`serialize_items`（会话转文本）、`ModelClient` + `extract_text`、类型。

**关键设计**：这个模块**不碰文件系统**。所有读写都通过 `HarnessStore` / `apply_proposal` 走存储层。refine.py 是"策略层"——决定提什么、怎么问模型、要不要并发检查；harness.py 是"机制层"——决定怎么落盘、怎么回滚。这样 `/refine` 和 `harness` 工具能共用同一个 `apply_proposal`，两者的历史记录和回滚能力天然一致。

两种工作模式贯穿全文：

| 模式 | 触发 | 调模型 | CAS 检查 | 目标作用域 |
|---|---|---|---|---|
| 普通提案 | `/refine [指令]` 或自动 | 是 | **是** | local（默认）或 global |
| 回滚 | `/refine rollback <id>` | **否** | 否 | 由历史记录里的 `scope` 决定 |

回滚**不调模型**（第 9 节详述）：它只是把历史记录里的 `before` 快照反向重放，是个纯本地操作，也不会失败在"模型没输出合法 JSON"上。

## 2. `REFINEMENT_INSTRUCTIONS`：提炼模型的 system 指令（26–57 行）

一整段英文 prompt，是这次调用的"宪法"。它解决三件事：

**第一，限定可改对象。** 开头就划死边界：

> The base system prompt is immutable. You may only Create, Update, or Delete:
> - prompt: narrow behavioral policy addendums (how the agent should act)
> - memory: durable facts, decisions, failures, preferences, project knowledge

这就是"提取成 prompt 类还是 memory 类"的答案——**由模型按语义判断，指令只给定义**：

- `prompt` = 行为策略，窄的、附加的（"以后遇到 X 应该这样做"）；
- `memory` = 事实与决策（"这个项目用 pytest 不用 unittest"、"用户偏好中文回复"）。

指令里的措辞是刻意的：`narrow behavioral policy addendums`（窄的行为策略**附加条款**）——强调 harness 是基础 system prompt 的增补，不是改写。这条防线在存储层还有一道：`_validate_edit` 会硬拒 `id == "base_system_prompt"` 的编辑（见 [harness.md](harness.md)），指令层的"immutable"和代码层的硬拒互为双保险。

**第二，教模型选作用域。** local 是默认，global 要跨会话复用才用：

> Local (default): session-specific notes, current-run coordination, project facts that should not leak to other sessions.
> Global: stable cross-session lessons, user preferences, or project-qualified facts likely reused later.

注意 "should not leak to other sessions"——local 存在的理由之一是**防止噪声外溢**。一次调试的临时结论写进 global 会污染所有后续会话，所以保守策略是默认 local。

**第三，作用域隔离规则**，这是最容易被忽略但很重要的一条：

> During a local refinement, treat global entries as read-only context. Create a local override instead of updating them.

local refine 时 global 条目**只读**。模型想改 global 里的某条，不能直接 update，得新建一条 local 覆盖。理由在 [harness.md](harness.md) 的 `merge_states`：local 和 global 合并成提示时，同 id 的 local 条目会以 `local:` 前缀保留、两边都在——所以"local 覆盖"是并存而非覆盖语义，写 global 会踩到别的会话。

最后一段是输出约束：`Output JSON only` + 一个 schema 例子。schema 里四个顶层字段：

| 字段 | 作用 |
|---|---|
| `summary` | 一句话摘要 → 进历史记录的 `trigger` |
| `rationale` | 为什么这些编辑有轨迹证据支撑 → 进 `evidence` |
| `expectedOutcome` | 期望改善什么、怎么验证 → 进 `outcome` |
| `edits[]` | 编辑列表，每项含 `action` / `kind` / `id` / `title` / `content` / `path` / `reason` |

三个文字字段不只是给人看的：它们写进 `refinements.jsonl`，**回滚时就靠 `summary`（即 `trigger`）在 `/refine rollback <id>` 里定位**。要求模型写 `expectedOutcome` 也是个自我约束——逼它先想清楚这条经验能验证，减少"看起来有道理"的垃圾条目。

## 3. 两个常量：`CONVERSATION_CHARS` 与 `TRUNCATED_JSON`（58–64 行）

```python
CONVERSATION_CHARS = 80_000
TRUNCATED_JSON = "the model stopped before completing its JSON object; retry with a smaller request"
```

`CONVERSATION_CHARS = 80_000`：发给提炼模型的会话文本上限（字符）。约 20k token，留出足够空间给 system 指令、harness 概览、历史和 JSON 输出。超过就**只留尾部**（第 8 节）——理由是近期对话比开头更可能包含值得记的结论（开头常是任务描述和探索，结尾是修正和定论）。

`TRUNCATED_JSON` 是错误消息文本，被 `_complete_json`（无输出时）和 `_parse_json`（括号没闭合时）共用。措辞里带 `"retry with a smaller request"`——它面向用户，说明这类失败**可重试**，不是模型逻辑错了。

## 4. 自动 refine 的节奏控制（66–84 行）

两个纯函数，把"什么时候自动 refine"从 UI 里抽出来做可测逻辑：

```python
def parse_auto_refine_every(raw: str | None = None) -> int:
    text = (raw if raw is not None else os.getenv("WHEEL_AUTO_REFINE", "8")).strip().lower()
    if text in {"0", "false", "no", "off"}:
        return 0
    if text in {"", "on", "true", "yes"}:
        return 8
    try:
        return max(0, int(text))
    except ValueError:
        return 8
```

`parse_auto_refine_every`（66–77 行）解析 `WHEEL_AUTO_REFINE` 环境变量，**缺省 8**。三个设计点：

- `0` / `off` / `false` / `no` 都识别为关闭——用户表达"关掉"的方式很多；
- `on` / `true` / `yes` / 空串 落到默认 8，而不是报错——**宽容解析，坏值不炸**；
- `int()` 失败也回落到 8，且 `max(0, ...)` 挡住负数。这个函数**永远返回合法值**，调用方不用 try。

```python
def refine_due(user_turns: int, every: int, last_at: int) -> bool:
    if every <= 0 or user_turns < every:
        return False
    return user_turns - last_at >= every
```

`refine_due`（79–84 行）是纯函数：**累计用户轮数 − 上次 refine 时的轮数 ≥ every**。用差值而非取模，好处是 last_at 可以是任意历史值（改了节奏也不会错位）。`user_turns < every` 这道门槛保证第一次触发也要攒够 N 轮，不会一进来就 refine。

调用方在 `ui/app/refine.py` 的 `maybe_schedule_periodic_refine`：每回合查一次，到期就把 `STATE.refine_at[session_id] = n` 记下再排后台线程——**先记账再干活**，避免线程还没跑完下一回合又排一次。

## 5. `parse_refine_args`：命令参数解析（86–105 行）

```python
def parse_refine_args(args: str) -> dict[str, Any]:
```

返回 `{"instructions": str|None, "global": bool}` 或 `{"rollback_id": str, "global": bool}`。三种形态：

| 输入 | 输出 |
|---|---|
| `""` | `{"instructions": None, "global": False}` |
| `只记跨会话的经验 --global` | `{"instructions": "只记...", "global": True}` |
| `rollback refine_2026...` | `{"rollback_id": "refine_...", "global": False}` |

解析顺序值得注意：`--global` 只认**开头前缀**（`rest.startswith("--global")`），剥离后剩下的整段都是 instructions。这样 `--global` 不会被当成指令文本的一部分。

回滚分支支持两种写法：`--global rollback <id>`（前缀先剥离）和 `rollback <id> --global`（后缀再剥离，96–99 行）。两种都收，因为用户敲命令的顺序是随机的。

两个错误出口都抛 `ValueError("usage: /refine rollback <id>")`：裸 `rollback` 不带 id（`if rest == "rollback"`，92 行），以及 id 缺失或只剩 `--global`。抛异常而非返回错误码——UI 层统一 `except ValueError` 打印用法（见 [ui/app-refine.md](../ui/app-refine.md) 的 `handle_refine`）。

## 6. `extract_json_object`：从回复里捞 JSON（107–130 行）

模型输出 JSON 这件事从来不可靠，这里是三级回退链：

1. **```json 围栏**：正则 `r"```(?:json)?\s*([\s\S]*?)```"` 抓围栏内容。模型最爱包围栏。
2. **整段就是对象**：`startswith("{") and endswith("}")` 直接解析。
3. **窗口兜底**：取第一个 `{` 到最后一个 `}` 的窗口试解析（119–126 行）。模型常在 JSON 前后加废话（"好的，这是我提炼的经验：...这是结果"），窗口能救回来。

第三级的失败处理有个细节：

```python
        except json.JSONDecodeError:
            return _parse_json(trimmed[start:])   # 可能是尾部被截，走 _parse_json 报截断错
```

窗口解析失败时，不是放弃，而是把**从 `{` 到字符串末尾**的整段交给 `_parse_json`——注意这里传的是 `trimmed[start:]` 而非窗口。这么做是为了让 `_parse_json` 的截断检测（第 12 节）看到真实的未闭合尾部，从而报"截断"而非"格式错"。**这两条错误对用户的意义完全不同**：截断 → 重试可能成功；格式错 → 重试也白搭。

空输入直接 `raise ValueError("refiner returned no text")`。

## 7. `normalize_proposal`：提案归一化（132–142 行）

```python
record = value if isinstance(value, dict) else {}
```

模型返回什么都敢往里塞，这里把它强制成标准结构：非 dict 输入 → 空记录；`edits` 不是 list → 空列表；`summary` 不是 str → 默认文案 `"Refined continual harness"`；`edits` 里的**非 dict 元素直接丢掉**（`[edit for edit in edits if isinstance(edit, dict)]`）。

要点：**这里不做字段级校验**。缺 title、action 不合法这类问题留给 `apply_proposal` → `_validate_edit` 处理，逐条 edit 标记失败而不是整份提案报废。这样一条坏 edit 不会让九条好 edit 一起丢。

## 8. `plan_refinement`：生成提案（144–189 行）

签名返回四元组 `(proposal, refinement_id, rollback_of, usage)`——**回滚时 `usage` 是空 `Usage()`、`rollback_of` 是被回滚的 id；普通模式 `rollback_of` 是 `None`**。用同一个返回类型承载两种模式，调用方不用分支。

### 回滚分支（162–167 行）

```python
if rollback_id:
    target = next((item for item in history if item.get("id") == rollback_id), None)
    if target is None:
        raise ValueError(f"refinement {rollback_id} not found")
    return rollback_proposal(target), refinement_id, target["id"], usage
```

三件事：在历史里按 id 找记录 → 找不到报错 → 用 `rollback_proposal` 反推 edits。**完全不调模型**。注意 `refinement_id` 是**新生成的**——回滚本身也是一次 refine，它有自己的 id，所以**回滚可以被再回滚**。

### 普通分支：拼 prompt（169–188 行）

prompt 由五个 XML 风格的分块拼成，用 `join` 且**过滤掉空 part**（`if part`）：

| 分块 | 内容 |
|---|---|
| `<current_harness_state>` | `_overview(state)`：现有条目（第 13 节） |
| `<refinement_history>` | `_history_for_prompt(history)`：最近 20 次 refine |
| `<conversation>` | `serialize_items(items)[-CONVERSATION_CHARS:]`：会话文本（尾部截断） |
| `<scope_policy>` | 作用域策略，随 `global_` 切换 |
| `<user_refine_instructions>` | 用户的 `/refine` 指令（**没有就整块不出现**） |

用 XML 标签而非 Markdown 标题分块：模型对标签边界的识别更稳，也避免会话文本里的 `#` 标题污染结构。

`scope_policy` 两条文案（172–176 行）是第 2 节 system 指令里作用域规则的**二次强调**：

- global：`"Only propose stable cross-session lessons, durable user preferences, or explicitly project-qualified facts."`
- local：`"Prefer session-specific notes. Global entries are read-only; create a local entry instead of updating them."`

同一条规则在 system 和 user prompt 里各说一遍——system 指令长、容易被模型忽略，放在靠近内容的位置复述一次更可靠。

结尾固定一句 `Return only JSON edits. If no useful edit is justified, return an empty edits array with a rationale.` ——**明确允许"什么都不改"**。这很重要：不给这个出口，模型会为了显得有用而编造条目，harness 库会被低质笔记灌满。

收尾：`text, usage = _complete_json(model, prompt)`，然后 `normalize_proposal(extract_json_object(text))`。三步流水线：捞 JSON → 归一化 → 返回。

## 9. `run_refine`：端到端执行（191–236 行）

这是对外主入口，UI 层（[ui/app-refine.md](../ui/app-refine.md) 的 `_execute_refine`）唯一调用的函数。

### 合并两个作用域的历史（203–207 行）

```python
by_id = {item["id"]: item for item in load_history(store.history_file(True))}
for item in load_history(store.history_file(False)):
    by_id[item["id"]] = item
history = list(by_id.values())
```

先 global 后 local，**同 id 时 local 覆盖 global**，用 dict 去重。为什么合并：`/refine rollback <id>` 用户不该先猜这条 refine 是 local 还是 global——合并后一个 id 空间搞定。`run_refine` 里查一次（209–213 行，决定 `apply_global`），`plan_refinement` 里再查一次（162 行）——略有重复但换来了解耦：`plan_refinement` 可独立测试。

### 回滚时的作用域重定向（208–213 行）

```python
apply_global = global_
if rollback_id:
    hit = next((item for item in history if item.get("id") == rollback_id), None)
    ...
    apply_global = hit.get("scope") == "global"
```

**回滚忽略命令行的 `--global`，用被回滚记录自己的 `scope`**。回滚必须写回原处才有意义——用户敲 `--global` 只是习惯性加的，跟着它走会把回滚写到错误的库里。

### CAS 基线与并发检查（215–236 行）

```python
target = store.target(apply_global)
merged = store.merged()
baseline = snapshot_state(target)   # 规划前的快照
proposal, refinement_id, rollback_of, usage = plan_refinement(...)
result = apply_proposal(
    target,
    proposal,
    ...
    baseline=None if rollback_id else baseline,   # 回滚不做并发检查
)
store.record(result)
```

这就是 README 里说的 **CAS（compare-and-swap）基线**，整段流程：

1. `store.target(apply_global)` 拿写目标。注意 `HarnessStore.target` 在 `global_=True` 且非交互模式时会**抛 "global harness writes are interactive-only"**——无人值守的自动 refine 不能改全局库，这是道硬闸。
2. `store.merged()` 合并视图给模型看（模型需要看到 global 才知道该不该建 local override），但**写只写 `target`**。读写对象不同：看全貌、改一处。
3. `snapshot_state(target)` 在**调模型之前**拍深拷贝快照。这步必须在规划前——模型调用是网络往返，几百毫秒到几秒，期间条目完全可能变（用户手动 `/harness` 改、上一次自动 refine 的线程刚落盘）。
4. `apply_proposal` 逐条比对：某条 entry 的 `asdict` 与 baseline 不一致 → 该 edit 标记 `"entry changed during refinement planning"` 失败并跳过，**不覆盖新状态**（比对细节见 [harness.md](harness.md)）。
5. `baseline=None if rollback_id else baseline`——**回滚不做并发检查**。回滚是恢复操作，它写的就是基线里的内容，此时再检查等于自己跟自己比；而且回滚的价值恰恰在于"不管现在变成什么样，都退回那一刻"。
6. `store.record(result)`：追加历史 + 置 `store.dirty = True`（主循环见到 dirty 会重拼 system prompt 并 bump cache epoch，见 [docs/loop-explained.md](../loop-explained.md) 第 10 节）。

返回 `(result, usage)`——usage 单独返回是因为这次模型调用**不在主循环的 usage 账本里**，UI 层要自己 `session.usage.add(extra)` 记进去（[ui/app-refine.md](../ui/app-refine.md) 的 `handle_refine` 和 `flush_auto_refine` 都这么做）。

## 10. `format_refine_result` 与 `_edit_text`：结果渲染（238–269 行）

把 result 字典渲染成人看的文本。行首标记沿用 harness 工具输出的约定：`+` 成功、`!` 失败。

结构：第一行 `local refine_xxx: <summary>`；有 `rollbackOf` 加一行；有 `rationale` 加一行；然后逐条成功 edit 打印 `+ action kind:id` 加标题和内容；再逐条失败 edit 打印 `! action kind:id <error>`；**两个列表都空时输出 `"  (no edits)"`**——这是最常见的正常结果（模型判断没东西值得记），必须让用户看到"跑过了但没提炼"而不是一片空白。

`_edit_text`（261–269 行）取标题和内容，**优先 `after` 再 `before`**（`source = after or before`）：成功 edit 有 `after`，失败的回滚类只有 `before`，回退到 `row.get("title")` 兜底。

## 11. `_complete_json`：关推理档的模型调用（271–292 行）

```python
old_effort = getattr(model, "effort", None)
if old_effort is not None:
    model.effort = "off"
try:
    response = model.complete(
        [{"role": "user", "content": prompt}],
        tools=[],
        instructions=REFINEMENT_INSTRUCTIONS,
    )
finally:
    if old_effort is not None:
        model.effort = old_effort
```

**核心取舍：refine 强制关推理档（`effort="off"`）。** 理由在 docstring 里——"refine 是后勤活"。提炼经验不需要链式推理，开推理档只是多花钱多等十几秒；而且推理模型的思维链会让输出更啰嗦，降低 JSON 合规率。

恢复用 `try/finally` 而非 `try/except`：即使调用抛异常也要还原 effort。为什么必须还原？因为 **model 客户端和主循环共享**（`ui/app/refine.py` 里虽然用 `make_client(..., effort="off")` 单独建了客户端，但 `run_refine` 允许传入主循环那个客户端）。不还原的话，一次 `/refine` 会把后续所有正常任务的推理档拖到 off。

`getattr(model, "effort", None)` 的宽容写法：客户端没有 `effort` 属性就跳过整个开关逻辑（不设也不恢复）。

调用参数：`tools=[]`（提炼不需要工具）、`instructions=REFINEMENT_INSTRUCTIONS`、单条 user 消息。输出空则 `raise ValueError(TRUNCATED_JSON)`——模型只输出了思考/未输出正文，也算截断。

## 12. `_parse_json` 与 `_incomplete`：区分截断和格式错（294–329 行）

```python
def _parse_json(candidate: str) -> dict[str, Any]:
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        if _incomplete(candidate):
            raise ValueError(TRUNCATED_JSON) from exc
        raise ValueError(f"the model did not return valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("refiner JSON must be an object")
    return value
```

两条错误消息指向两种完全不同的处置：截断 → 会话再长一点/重试也许能行；格式错 → 模型这次就是没按要求输出。**对用户来说这两条必须分开**，否则会把"模型瞎输出"误判成"内容太多"。

`_incomplete`（307–329 行）是个手写小状态机，判断括号/引号是否闭合：`depth` 记 `{}[]` 嵌套深度，`in_string` 记是否在字符串内，`escaped` 处理 `\"`。三个状态的顺序有讲究——`escaped` 最先判（`\"` 不该退出字符串），`in_string` 次之（字符串里的 `{` 不计深度）。返回 `in_string or depth > 0`：任一为真就是没写完。

为什么不用现成方案（比如 `json.JSONDecoder.raw_decode` 看能否解析）：raw_decode 分不出"截断"和"格式错"，两者都是解析失败。这个状态机就是为了拿到这个区分。

## 13. `_overview` 与 `_history_for_prompt`：给模型看的上下文（331–359 行）

```python
def _overview(state: HarnessState) -> str:
```

当前 harness 状态概览。每行格式：

```
- [scope:id] title (path, vN): content
```

三个细节：

- content 先 `re.sub(r"\s+", " ", ...)` 压成单行再截到 240 字符——多行内容会破坏 prompt 结构；
- **带上 `vN` 版本号**：让模型知道这条被改过几次，间接提示"改过多次的条目别再动"；
- **每类只列 40 条**，超出打 `- +N more <kind> entries`。40 比提示注入用的 `MAX_ENTRIES_PER_KIND = 8` 大五倍——提炼时模型需要看到全貌才能判断重复，但也不能无限放（harness 库可能很大）。空状态返回 `"No saved harness entries yet."` 而不是空串，避免模型面对空分块瞎猜。

```python
def _history_for_prompt(history: list[dict[str, Any]]) -> str:
```

最近 20 条 refine 历史。每条三行：

```
[id][ rollbackOf=xxx] summary
applied create prompt:xxx, failed update memory:yyy
Expected outcome: ...
```

两个作用：

1. **防重复提炼**——模型看到"这条经验上次已经记过了"，就不会再提一次。这是 harness 库不膨胀的主要防线。
2. **暴露失败模式**——`failed` 前缀的行告诉模型"上次这类编辑被拒了"（比如并发冲突、条目不存在），减少重复犯错。

`rollbackOf=` 只在有值时出现（三元表达式），让模型知道哪些 id 是回滚产生的、别再去回滚它。

空历史返回 `"No prior refinement history."`。

---
