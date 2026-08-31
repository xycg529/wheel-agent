# `core/prompt.py` 逐段讲解

> 本篇讲系统提示与每轮临时上下文的组装。上游是 [core/loop.py](../../../core/loop.py)（唯一调用方），下游是 [core/context.py](context.md)（文件扫描与 XML 渲染）、[harness/harness.py](../harness/harness.md)（笔记渲染）、[core/plan.py](plan.md)（计划状态）。

把工作区规则、项目 skill、harness 笔记拼成 system prompt，另产出每轮一换的临时 system 消息（日期/工作目录/计划）。

- 行数：100 行
- 依赖：
  - [core/context.py](context.md) —— `load_project_files` / `load_skills` / `format_project_xml` / `format_skills_xml` / `today`（实际的扫描与渲染都在这里，本文件只做拼装）
  - [harness/harness.py](../harness/harness.md) —— `HarnessState` 与 `format_harness_for_prompt`（跨任务笔记的渲染）
  - [core/plan.py](plan.md) —— `PlanStore`（临时上下文里的计划状态来源）
- 被谁用：
  - [core/loop.py](../../../core/loop.py) 第 95–96 行（开机）、188 行（每轮刷新临时项）、245/251 行（harness 变脏后重拼）——**只有主循环调用，别处不用**

## 目录

- [1. 模块 docstring 与导入（1–10 行）](#1-模块-docstring-与导入110-行)
- [2. `EPHEMERAL_MARK` 常量（12–13 行）](#2-ephemeral_mark-常量1213-行)
- [3. `system_prompt` 签名与 docstring（16–27 行）](#3-system_prompt-签名与-docstring1627-行)
- [4. 第一块：固定行为指令（28–56 行）](#4-第一块固定行为指令2856-行)
- [5. 第二块：项目指令文件（57–60 行）](#5-第二块项目指令文件5760-行)
- [6. 第三块：skills 与 `trusted`（61–65 行）](#6-第三块skills-与-trusted6165-行)
- [7. 第四块：harness 笔记（66–68 行）](#7-第四块harness-笔记6668-行)
- [8. 拼装输出（69 行）](#8-拼装输出69-行)
- [9. `ephemeral_items` 签名（70–78 行）](#9-ephemeral_items-签名7078-行)
- [10. 固定三行（79–83 行）](#10-固定三行7983-行)
- [11. 计划块与三种状态提示（84–99 行）](#11-计划块与三种状态提示8499-行)
- [12. 返回形态（100 行）](#12-返回形态100-行)

---

## 1. 模块 docstring 与导入（1–10 行）

docstring 点明两个产出物：

- **system prompt**（长期、稳定）——`system_prompt()`；
- **每轮 ephemeral 上下文**（临时、每轮重算）——`ephemeral_items()`。

导入只有三行，各自代表一个数据来源。本文件**不含任何扫描/读取/排序逻辑**，只负责"把别人渲染好的文本块按顺序粘起来、中间空一行"。职责边界很干净：想改"哪些文件被收集"去 context.py，想改"提示里写什么约束"才来这里。

## 2. `EPHEMERAL_MARK` 常量（12–13 行）

```python
EPHEMERAL_MARK = "[ephemeral context — not a user message]"
```

临时上下文以 `role: "system"` 注入，但内容里会出现"当前日期""计划已批准"这类陈述句。不标注的话，模型（以及读日志的人）容易把它当成一条用户指令。这个标记是给模型看的免责声明：**这不是用户说的，不要当新需求执行**。

同理，[core/compact.py](compact.md) 里的摘要消息用 `SUMMARY_MARK` 做同样的事——两个标记是同一个手法。

## 3. `system_prompt` 签名与 docstring（16–27 行）

```python
def system_prompt(workspace, home=None, *, trusted=True, harness=None) -> str
```

- `home`：用户级目录（`~/.wheel/`），默认 `Path.home()`，透传给 context.py 找用户级 `AGENTS.md` 和 `~/.wheel/skills`。**只有测试才会显式传它**，主循环不传。
- `trusted`：唯一影响内容裁剪的开关（见第 6 节）。
- `harness`：合并后的 `HarnessState`（loop 传入 `store.merged()`）。为 `None` 时整块不出现——所以"没有 harness"和"harness 为空"在提示里是两回事（前者无块，后者有一块 `No saved entries.`）。

docstring 里有一句警告值得当真：

> 文本里嵌着对模型的关键约束……改动这些句子会影响模型行为。

这个函数的返回值是**纯字符串、没有任何结构**，改一个词就可能改变 agent 行为，且没有单元测试能兜住。这是全项目最"手感"的一处代码。

## 4. 第一块：固定行为指令（28–56 行）

一个硬编码的长字符串，是提示的**第一块也是最长的一块**。它是纯文本的、不带任何模板变量（除 `Workspace: {root}`）。内容可以拆成六组约束：

| 约束组 | 关键句子 | 为什么要写进提示 |
|---|---|---|
| 边界 | `Work only inside the workspace` + 工作区绝对路径 | 模型没有内置"当前目录"概念，工具层靠 `Workspace` 限制，提示层再强调一次 |
| 工具清单 | 一行列全部 13 个工具名 | Responses API 的 tools schema 里已有描述，但模型容易忽略冷门工具（`bash_kill`、`web_fetch`），列一遍提高调用率 |
| 工具语义 | `ls lists one directory` / `glob ... (ripgrep --files)` / `edit with unique old_string` | 纠偏：`ls` 不是递归，`glob` 只匹配文件名不搜内容，`edit` 的 `old_string` 必须唯一 |
| bash 纪律 | 前台 120s 超时；安装/测试/起服务**必须** `background=true`；拿到 `job_id` 就**停止本轮** | 最重要的行为约束。没有它，模型会对 `npm install` 死等到超时，或陷入 `bash_poll` 循环烧 token |
| plan 纪律 | 提到计划就**必须**调 `plan` 工具；一次只标一个 `in_progress`/`done`；计划被拒则本轮结束、不等确认 | 见下 |
| 破坏性命令 | 用户要 `rm` 就照调 bash，**不许在正文里拒绝**；harness 会拦；`rm`/`sudo` 不得自发使用 | 见下 |

**plan 纪律为什么这么长**（占了整块近一半篇幅）：纯靠工具返回的错误信息纠正不了两种失败模式——① 模型在聊天正文里写一份 markdown 计划就当交差（提示明说 `A markdown plan in your chat reply is not a substitute`）；② 模型把整个计划一次性标完 `done`，进度不可见。这些是实测出来的行为偏差，只能靠提示词压。计划状态机本身在 [core/plan.py](plan.md)。

**破坏性命令为什么要求"照调不误"**：把拒绝权从模型手里拿走。模型倾向于在正文里说"这个命令有风险，我不能执行"，导致用户想做的事根本走不到安全门。这里强制它**照常调用 bash**，由 [tools/safety.py](../tools/safety.md) 弹 y/N——拒绝发生在工具层，模型只会收到一条 error 结果，它就能据此继续（改命令或请用户确认）。

## 5. 第二块：项目指令文件（57–60 行）

```python
project = format_project_xml(load_project_files(root, home=home))
if project:
    parts.append(project)
```

`load_project_files`（[context.md](context.md)）从工作区向上走到 git 根，逐级取 `AGENTS.override.md` > `AGENTS.md` > `CLAUDE.md` 里第一个存在者，再把用户级 `~/.wheel/AGENTS.md` 插在队首（全局优先）。`format_project_xml` 把每份文件包成 `<project_instructions path="...">`。

注意这里**没有 `trusted` 判断**——项目指令文件（AGENTS.md）不受信任门控，只有 skill 受控。差别在于：AGENTS.md 是用户自己仓库里的常规文件，模型本来也能用 `read` 读到（提示注入的风险不因注入提示而增加）；而 skill 目录是"自动发现并注入"的机制，不可信仓库可以借此把任意文本塞进系统提示。

空则整块跳过（`format_project_xml` 在 `files` 为空时返回 `""`），避免给提示增加空标签噪音。

## 6. 第三块：skills 与 `trusted`（61–65 行）

```python
skills = format_skills_xml(load_skills(root, home=home, trusted=trusted))
if skills:
    parts.append(skills)
```

`trusted` 唯一的用途就是透传给 `load_skills`。在 [context.py](context.md) 里：

- `trusted=True`：扫描工作区各级的 `.wheel/skills`、`skills/`、`.agents/skills`，**加上**用户级 `~/.wheel/skills`、`~/.agents/skills`；
- `trusted=False`：**只**加载用户级那两个目录。

用户级目录始终加载——它是用户自己 home 下的文件，和"不可信工作区"无关。

`trusted` 从哪来（loop.py 第 94 行）：

```python
trusted = is_trusted(ws.root) or not project_skill_dirs(ws.root)
```

两个条件任一成立即视为可信：显式 allow（记在 `~/.wheel/trust.json`）**或**工作区压根没有项目 skill 目录（没东西可注入）。询问用户发生在 UI 启动时的 `ensure_project_trust`（[tools/trust.py](../tools/trust.md)），主循环自己从不弹窗。

实测差异：一个含单个 skill 的工作区，`trusted=False` 时提示里没有 `available_skills` 块，整体短约 425 字符。

`format_skills_xml` 只放**元数据**（name / description / location），不放正文——正文靠用户敲 `/skill:name` 时由 `expand_skill_command` 按需注入。这样 20 个 skill 也只占几十行提示。

## 7. 第四块：harness 笔记（66–68 行）

```python
if harness is not None:
    parts.append(format_harness_for_prompt(harness))
```

`HarnessStore.merged()` 把 global（`~/.wheel/harness/`）+ local（会话级）合并成一个视图，同 id 时 local 那份加 `local:` 前缀、两边都保留。渲染时每类（prompt/memory）最多 8 条、单条正文截断到 240 字符，并按 `(path, title, id)` **稳定排序**——排序稳定是为了提示前缀能复用 prompt cache，见 [harness/harness.md](../harness/harness.md)。

渲染块开头写死一句：

> `The base system prompt is immutable.`

这是防模型钻空子的：harness 笔记是**模型自己**通过 `harness` 工具写进去的（[tools/tools.py](../../../tools/tools.py)），没有这句，模型可能试图"写一条笔记来覆盖系统提示"。这句把笔记定性为"追加约束，不可覆盖基础指令"。

## 8. 拼装输出（69 行）

```python
return "\n\n".join(parts)
```

四块之间空一行。实测空工作区（无 AGENTS.md、无 skill、harness 为空）得到 2 段：固定指令 + harness"无条目"块。**顺序是固定的**：固定指令 → 项目指令 → skills → harness。越靠后越"容易被改"：固定指令改代码才能改，项目指令用户改个文件就变，harness 模型运行时就能改。

顺序也影响 prompt cache：内容一变，从变化点起后面的缓存全失效。所以**易变的内容放后面**是刻意的。harness 变脏时 loop 会自增 `cache_epoch` 并重拼提示（loop.py 第 236–243 行）——正是因为这块内容在提示末尾，改它会让后续全部失效，不如直接开新纪元。

## 9. `ephemeral_items` 签名（70–78 行）

```python
def ephemeral_items(workspace, plan: PlanStore | None = None) -> list[dict[str, str]]
```

返回**一个元素的 list**（不是字符串）——因为它要直接拼进 `items + extra` 里发给模型，元素形态必须是 item dict。

docstring 一句话讲清设计：

> 只影响本轮、不进历史——这样每轮的日期/计划状态总是新鲜的，又不破坏历史前缀缓存。

两个理由，第二个更硬：

1. **新鲜度**：日期会变、计划状态会变。若把"当前日期"作为普通历史消息存下来，跨天续会话时历史里会留着三天前的日期，和本轮那条打架。
2. **前缀缓存**：临时项若进历史，每轮内容一变，它之后的所有内容都失去缓存；放在最后（`items + extra`）则只影响本轮请求，历史前缀（系统提示 + 旧消息）的 cache 完全不动。这是 [README](../../../README.md) "前缀缓存策略"一节的做法在这里的落地。

loop.py 里 `extra_input` 刷新的三个位置：开机（96 行）、每轮模型返回后（188 行）、harness 变脏后（251 行）。传模型时走独立参数 `extra=extra_input`（loop.py 第 160–162 行），所以 `_push` 永不碰它——**不进历史是结构保证的，不是靠自觉**。

## 10. 固定三行（79–83 行）

```python
lines = [
    EPHEMERAL_MARK,
    f"Current date: {today()}",
    f"Current working directory: {root}",
]
```

- `today()` 来自 context.py，返回本地时区的 `YYYY-MM-DD`（不是 UTC——按用户体感给日期）。
- `root` 是 `Path(workspace).resolve()`。注意 resolve 后 macOS 上 `/tmp` 会变成 `/private/tmp`，和 `Workspace` 里存的路径是同一个来源，不会不一致。

模型没有"今天几号"的可靠内知，涉及"最近改动""日志时间戳"的判断全靠这一行。

## 11. 计划块与三种状态提示（84–99 行）

仅在 `plan.steps` 非空时出现，分两层：

```python
lines.append("<plan>")
lines.append(plan.render())     # [ ]/[>]/[x] 编号清单
lines.append("</plan>")
```

`plan.render()` 输出 `[ ] pending` / `[>] in_progress` / `[x] done` 的复选框清单（[plan.md](plan.md)）。用 XML 标签包起来，是为了和周围的散文区分开，也方便模型整块引用。

之后按 `confirmed` / `rejected` / 两者皆非（待确认）三态给一句**行为指令**：

| 状态 | 条件 | 指令要点 |
|---|---|---|
| 已批准 | `plan.confirmed` | 继续做 pending/in_progress 步骤，**不要再等一次 yes**；一次只标一个 |
| 已拒绝 | `plan.rejected` | 把用户下一条消息当作对计划的反馈；**在计划工具返回 approved 之前不许写/编辑文件** |
| 待确认 | 其余 | 这是会话状态，**要提交给 plan 工具**让 harness 问用户 |

三态的必要性：计划状态存在 `PlanStore` 里（内存对象），模型看不到。工具返回只在"调用的那一轮"可见，跨轮就丢了。每轮重投一份，等于**给模型一个无状态的状态同步通道**。

"已批准"那句的 `Do not wait for another yes` 是实测补丁：模型拿到 approved 后仍倾向于在正文里问"可以开始了吗？"，白等一轮。

`plan_rejected` 的停止判定不在这里——loop.py 第 256–258 行靠工具结果里含 "rejected" 来判断并停机，这里只是提醒模型接下来该怎么做。

## 12. 返回形态（100 行）

```python
return [{"role": "system", "content": "\n".join(lines)}]
```

`role: "system"` 而非 `"user"`，配合开头的 `EPHEMERAL_MARK` 双重声明"非用户消息"。

跨协议都能工作：[core/model.py](model.md) 的 `items_to_chat_messages` 把 `role == "system"` 的 item 原样转成 Chat 的 system 消息（第 302–305 行）；Responses 协议下 extra 直接进 `input` 数组，system 角色同样合法。两种 API 都不需要特殊处理。

---
