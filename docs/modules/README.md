# wheel-agent 模块讲解索引

> 读完 `README.md`（项目自介）和 `docs/loop-explained.md`（主循环逐段讲解）之后，从这份索引按阶段往下读，能把 30 篇模块文档串成一条线。

## 怎么用这份索引

阅读顺序按**代码的依赖方向**排：先主干（循环和它直接吃的东西），再上下文管理（喂给循环的），再工具与安全（循环调的），再持续学习（循环改的），最后终端与回放（包住循环的）。每篇文档开头都有「上游是 A，下游是 B」，读串了随时能找回位。

## 推荐阅读路线

### 阶段 0：骨架（30 分钟）

只读两篇，不看细节：

- [docs/loop-explained.md](../loop-explained.md) —— 主循环 18 段 + 生命周期图。**建立骨架：reason → act → observe。**
- 本文件的[「一条请求的完整生命周期」](#一条请求的完整生命周期) —— 20 步把全项目串起来。

读完能回答：一个任务从敲回车到结束，经过了哪些模块？

### 阶段 1：主干（2 小时）

循环直接吃/产的东西。读完能回答：模型响应怎么进来、token 怎么算、配置从哪来？

1. [core/types.md](core/types.md) —— 全项目共享的数据形状（Item / Usage / RunResult）。**最短的一篇，先读它，后面所有文档的字段名都对得上。**
2. [core/config.md](core/config.md) —— provider 发现与组装（`.env` → `AgentConfig`）。
3. [core/model.md](core/model.md) —— 最重的一篇：Responses / Chat Completions 两套协议归一、流式、重试、prompt cache。
4. [core/reasoning.md](core/reasoning.md) —— effort 档位的统一刻度与钳制（短）。
5. 回头重读 [docs/loop-explained.md](../loop-explained.md) 第 6 节（模型调用）——现在能看懂 `_complete_with_overflow` 每一步了。

### 阶段 2：上下文与持久化（2 小时）

循环每轮都在动的东西。读完能回答：历史怎么不爆、会话怎么分叉、事件怎么落盘？

1. [core/context.md](core/context.md) —— token 估算（不调 tokenizer 怎么算用量）。
2. [core/truncate.md](core/truncate.md) —— 单条消息内的裁剪（大工具输出截多少）。
3. [core/compact.md](core/compact.md) —— 整段历史的压缩（摘要 + 保留最近 + 文件清单合并）。
4. [core/session.md](core/session.md) —— 会话树（parent_id / leaf / 零拷贝分叉）与 JSONL 持久化。
5. [core/events.md](core/events.md) —— EventBus：emit 先落盘再喂订阅者、三个记录文件、`load_run` 三级查找。
6. [core/queue.md](core/queue.md) —— TurnQueue 三通道（steer/follow/abort）与 AskWaiter 跨线程 y/N（短）。

### 阶段 3：工具与安全（3 小时）

循环 act 阶段调的东西。读完能回答：工具怎么执行、危险操作怎么拦、undo 怎么做到不靠 git？

1. [tools/tools.md](tools/tools.md) —— 第二长的文件：工具注册表、`execute_batch`、逐个工具、bash 后台作业、`parse_function_calls`。
2. [tools/safety.md](tools/safety.md) —— 三级裁决 allow/ask/deny：只读白名单、敏感路径、bash 命令意图分析、批准记忆。
3. [tools/workspace.md](tools/workspace.md) —— Workspace 封装与越界防护（短）。
4. [tools/audit.md](tools/audit.md) —— 快照/指纹/diff 三件套、脱敏、环境指纹。
5. [core/checkpoint.md](core/checkpoint.md) —— 文件级 undo（不依赖 git 的快照栈）。
6. [tools/trust.md](tools/trust.md) —— 工作区信任与项目 skill 注入（短）。
7. [core/plan.md](core/plan.md) —— PlanStore 三态机与 plan 工具（短）。
8. [tools/rgfiles.md](tools/rgfiles.md) —— glob/grep 双实现（rg 优先、纯 Python 回退）。
9. [tools/web.md](tools/web.md) —— web 搜索/抓取。
10. [tools/atfiles.md](tools/atfiles.md) —— @ 文件引用补全（短）。

### 阶段 4：持续学习（1 小时）

循环会改的「笔记」。读完能回答：agent 怎么把经验带进下一个会话？

1. [harness/harness.md](harness/harness.md) —— local/global 两层作用域、prompt/memory 两种 kind、变更历史与回滚、`merged()` 进系统提示。
2. [harness/refine.md](harness/refine.md) —— `/refine` 流程：从对话提取经验、乐观并发、自动 refine。

### 阶段 5：终端与回放（2 小时）

包住循环的外壳。读完能回答：键盘输入怎么变成 steer、y/N 怎么跨线程、replay 怎么不发 API 重跑？

1. [ui/app.md](ui/app.md) —— TUI 主入口：启动、`run_task` 接线、忙时输入、斜杠分发、两段式 Ctrl+C。
2. [ui/repl.py](ui/repl.md) —— 行编辑器、按键读取、选择器、`BusyPrompt`。
3. [ui/app-live.md](ui/app-live.md) —— 事件流到屏幕：流式文本、工具进度、展开。
4. [ui/app-commands.md](ui/app-commands.md) —— 斜杠命令的薄适配器们。
5. [ui/app-state.md](ui/app-state.md) —— 共享状态（短）。
6. [ui/app-refine.md](ui/app-refine.md) —— `/refine` 的 UI 层。
7. [ui/replay.md](ui/replay.md) —— ScriptedModel 重跑与四态判定（exact/behavioral/drift/error）。
8. [ui/style.md](ui/style.md) —— ANSI、CJK 宽度、固定页脚（参考型，查用为主）。
9. [ui/markdown.md](ui/markdown.md) —— 终端 markdown 渲染（短）。
10. [ui/graph.md](ui/graph.md) —— 会话图与 `/graph html`。

## 模块速查表

### core（13 篇）

| 模块 | 源码 | 一句话职责 |
|---|---|---|
| [types](core/types.md) | [core/types.py](../../core/types.py) | 共享数据形状：Item / Usage / RunResult / SafetyVerdict |
| [config](core/config.md) | [core/config.py](../../core/config.py) | `.env` 发现 provider，组装 `AgentConfig` |
| [model](core/model.md) | [core/model.py](../../core/model.py) | 两套 API 协议归一、流式、重试、prompt cache、`ScriptedModel` |
| [loop](../loop-explained.md) | [core/loop.py](../../core/loop.py) | 主循环：reason → act → observe（单独成篇） |
| [context](core/context.md) | [core/context.py](../../core/context.py) | 不调 tokenizer 的 token 估算、skill/项目文件加载 |
| [truncate](core/truncate.md) | [core/truncate.py](../../core/truncate.py) | 单条消息内的裁剪（大工具输出） |
| [compact](core/compact.md) | [core/compact.py](../../core/compact.py) | 整段历史压缩成摘要，保留最近原文 |
| [session](core/session.md) | [core/session.py](../../core/session.py) | 会话树 + leaf 指针 + JSONL 持久化 + 缓存纪元 |
| [events](core/events.md) | [core/events.py](../../core/events.py) | EventBus：emit 落盘 + 喂订阅者，replay 的数据源 |
| [queue](core/queue.md) | [core/queue.py](../../core/queue.py) | TurnQueue（steer/follow/abort）+ AskWaiter |
| [plan](core/plan.md) | [core/plan.py](../../core/plan.py) | PlanStore 三态机，plan 工具的确认/拒绝 |
| [prompt](core/prompt.md) | [core/prompt.py](../../core/prompt.py) | 系统提示拼装（固定指令 + skill + harness + 临时项） |
| [reasoning](core/reasoning.md) | [core/reasoning.py](../../core/reasoning.py) | effort 档位统一刻度与钳制 |
| [checkpoint](core/checkpoint.md) | [core/checkpoint.py](../../core/checkpoint.py) | 文件级 undo 快照栈（task 边界） |
| [meter](core/meter.md) | [core/meter.py](../../core/meter.py) | 成本计算与页脚计量行 |

### tools（9 篇）

| 模块 | 源码 | 一句话职责 |
|---|---|---|
| [tools](tools/tools.md) | [tools/tools.py](../../tools/tools.py) | ToolRuntime、工具注册表、bash 后台作业、schema 生成、调用解析 |
| [safety](tools/safety.md) | [tools/safety.py](../../tools/safety.py) | 三级安全裁决 + 批准记忆 |
| [workspace](tools/workspace.md) | [tools/workspace.py](../../tools/workspace.py) | Workspace 封装、路径规范化与越界防护 |
| [audit](tools/audit.md) | [tools/audit.py](../../tools/audit.py) | 快照/指纹/diff、脱敏、环境指纹 |
| [trust](tools/trust.md) | [tools/trust.py](../../tools/trust.py) | 工作区信任判定、项目 skill 目录发现 |
| [rgfiles](tools/rgfiles.md) | [tools/rgfiles.py](../../tools/rgfiles.py) | glob/grep 双实现（rg 优先） |
| [web](tools/web.md) | [tools/web.py](../../tools/web.py) | web 搜索/抓取与正文提取 |
| [atfiles](tools/atfiles.md) | [tools/atfiles.py](../../tools/atfiles.py) | @ 文件引用补全 |

### harness（2 篇）

| 模块 | 源码 | 一句话职责 |
|---|---|---|
| [harness](harness/harness.md) | [harness/harness.py](../../harness/harness.py) | local/global 笔记存储、变更历史、回滚 |
| [refine](harness/refine.md) | [harness/refine.py](../../harness/refine.py) | 从对话提取经验写入 harness |

### ui（11 篇）

| 模块 | 源码 | 一句话职责 |
|---|---|---|
| [app](ui/app.md) | [ui/app/__init__.py](../../ui/app/__init__.py) | TUI 主入口：接线、分发、两段式 Ctrl+C |
| [repl](ui/repl.md) | [ui/repl.py](../../ui/repl.py) | 行编辑器、按键、选择器、BusyPrompt |
| [app-live](ui/app-live.md) | [ui/app/live.py](../../ui/app/live.py) | 事件流到屏幕的实时渲染 |
| [app-commands](ui/app-commands.md) | [ui/app/commands.py](../../ui/app/commands.py) | 斜杠命令薄适配器 |
| [app-state](ui/app-state.md) | [ui/app/state.py](../../ui/app/state.py) | 共享可变状态（STATE） |
| [app-refine](ui/app-refine.md) | [ui/app/refine.py](../../ui/app/refine.py) | `/refine` 的 UI 层与自动 refine 调度 |
| [replay](ui/replay.md) | [ui/replay.py](../../ui/replay.py) | 录制响应重跑与四态判定 |
| [style](ui/style.md) | [ui/style.py](../../ui/style.py) | ANSI、CJK 宽度、固定页脚（参考型） |
| [markdown](ui/markdown.md) | [ui/markdown.py](../../ui/markdown.py) | 终端 markdown 渲染 |
| [graph](ui/graph.md) | [ui/graph.py](../../ui/graph.py) | 会话图、ASCII 渲染、`/graph html` 服务 |

## 一条请求的完整生命周期

从 `wheel "修这个 bug"` 到任务结束，按时间顺序：

1. **入口**：`python -m wheel_agent.ui.app` → [ui/app.md](ui/app.md) `main()` → `session()`。
2. **配置**：[core/config.md](core/config.md) `load_config()` 读 `.env`、发现 provider；`ensure_project_trust`（[tools/trust.md](tools/trust.md)）确认工作区可信。
3. **会话**：[core/session.md](core/session.md) `Session.create()` 建会话树根；`Session.purge_empty` 清垃圾。
4. **界面**：[ui/style.md](ui/style.md) `Footer.arm()` 预留底部 3 行；[ui/app.md](ui/app.md) `print_chrome()` 打印横幅；[ui/repl.md](ui/repl.md) `LineEditor` 等输入。
5. **提交**：回车 → [ui/app.md](ui/app.md) `dispatch()` → `start_task()`：`expand_skill_command`（[core/context.md](core/context.md)）展开 `/skill:`；建 `TurnQueue`（[core/queue.md](core/queue.md)）；`busy_prompt.show()` 锚定 `>` 行；起工作线程。
6. **接线**：工作线程 [ui/app.md](ui/app.md) `run_task()`：`make_client`（[core/model.md](core/model.md)）带 `session.cache_key` 建模型客户端；`model.abort = queue.abort`；`on_delta` / `on_tool_update` / `on_event=print_event`（[ui/app-live.md](ui/app-live.md)）三个回调挂上。
7. **组装**：[core/loop.py](../loop-explained.md) `run_agent()`：`Workspace` / `EventBus`（[core/events.md](core/events.md)）/ `SafetyGate`（[tools/safety.md](tools/safety.md)）/ `PlanStore`（[core/plan.md](core/plan.md)）/ `HarnessStore`（[harness/harness.md](harness/harness.md)）/ `ToolRuntime`（[tools/tools.md](tools/tools.md)）；`begin_task()` 开 checkpoint 任务（[core/checkpoint.md](core/checkpoint.md)）；拍初始工作区指纹（[tools/audit.md](tools/audit.md)）；拼系统提示（[core/prompt.md](core/prompt.md)）。发 `agent_start`。
8. **循环（每轮）**：
   9. `turn_start` + 输入审计（上下文指纹 + 工作区指纹）。
   10. `_complete_with_overflow`（[core/loop.py](../loop-explained.md) 第 17 节）→ [core/model.md](core/model.md) `complete()`：发 API（带 `prompt_cache_key`）、流式回 `on_delta`、重试临时错误、归一化成 `ModelResponse`。上下文溢出时 `compact_history`（[core/compact.md](core/compact.md)）强制压缩后重试一次。
   11. `record_response` 把模型原始输出落盘（replay 的数据源）；输出 `_push` 进 `session.items`（[core/session.md](core/session.md)）。
   12. `parse_function_calls`（[tools/tools.md](tools/tools.md)）解析工具调用。
   13. **无调用**：消化排队的 steer/follow（[core/queue.md](core/queue.md)）；没有就 `stop` 收场。
   14. **有调用**：`_run_tools`（[core/loop.py](../loop-explained.md) 第 18 节）→ `SafetyGate` 裁决（[tools/safety.md](tools/safety.md)，ask 时经 `AskWaiter` 跨线程问 y/N，[core/queue.md](core/queue.md) + [ui/app.md](ui/app.md) `busy_wait`）→ 执行工具（[tools/tools.md](tools/tools.md)；write/edit 先备份，[core/checkpoint.md](core/checkpoint.md)；大输出截断，[core/truncate.md](core/truncate.md)）→ 结果回灌上下文 → harness 笔记脏了就重拼系统提示并 bump 缓存纪元（[harness/harness.md](harness/harness.md) + [core/session.md](core/session.md)）→ `session.persist()`。
15. **收场**：正常结束顺手 `compact_history`（[core/loop.py](../loop-explained.md) 第 13 节）；拍终局工作区指纹、算 `workspace_changes`（[tools/audit.md](tools/audit.md)）；发 `agent_end`；`write_meta` 落盘。
16. **回写**：[ui/app.md](ui/app.md) `_finish_session()`：`turn_offset += turns`、`usage.add()`、`persist(rewrite=True)`；页脚刷 [core/meter.md](core/meter.md) 计量行；`maybe_schedule_periodic_refine`（[ui/app-refine.md](ui/app-refine.md)）到点就后台 [harness/refine.md](harness/refine.md) 抽取经验。
17. **退出**：`/quit` 或两次 Ctrl+C → [ui/app.md](ui/app.md) `shutdown_ui()`：`abort_active()` 三层中止、`stop_graph_server`（[ui/graph.md](ui/graph.md)）、恢复 SIGWINCH、`Footer.disarm()`（[ui/style.md](ui/style.md)）。
18. **回放**（事后）：`/replay <id>` → [ui/replay.md](ui/replay.md) `load_run`（[core/events.md](core/events.md)）读 `responses.jsonl` → `ScriptedModel`（[core/model.md](core/model.md)）替换真模型 → 再走一遍第 7–15 步 → 四态判定。

## 核心概念索引

| 概念 | 一句话 | 讲得最深的地方 |
|---|---|---|
| item / 上下文 | `Item = dict[str, Any]`，模型能看到的全部历史 + 本轮临时项 | [core/types.md](core/types.md) 第 1 节 |
| turn vs step | `step` 是本 run 第几轮，`turn = turn_offset + step` 跨任务连续编号 | [loop-explained.md](../loop-explained.md) 第 5 节 |
| ephemeral items | 每轮重新生成的临时上下文（日期/计划），只进请求不进历史 | [core/prompt.md](core/prompt.md) |
| 紧凑 vs 截断 | compact 压整段历史成摘要；truncate 裁单条消息内的内容 | [core/compact.md](core/compact.md) + [core/truncate.md](core/truncate.md) |
| 会话树与 leaf | 每条记录带 parent_id，当前对话 = 根到 leaf 的路径，分叉 = 移动 leaf | [core/session.md](core/session.md) 第 2 节 |
| 缓存纪元 | `cache_epoch` 变 → `cache_key` 变 → `prompt_cache_key` 变 → 旧缓存失效 | [core/session.md](core/session.md) 第 5 节 |
| 安全裁决 | allow / ask / deny 三级，裁决来源 rules/memory/user，ask 被批准记忆覆盖 | [tools/safety.md](tools/safety.md) |
| harness 笔记 | local（会话）/ global（全局）两层，prompt（行为）/ memory（事实）两种，进系统提示 | [harness/harness.md](harness/harness.md) |
| run 与 replay | 一个 run = 一个 run_dir（events.jsonl + responses.jsonl + meta.json），replay 用 ScriptedModel 重跑 | [core/events.md](core/events.md) + [ui/replay.md](ui/replay.md) |
| steer / follow / abort | 运行中三条输入通道：下一轮注入 / 本轮停后投递 / 轮间停机 | [core/queue.md](core/queue.md) |
| task_id 与 undo | 每个 run 一个 checkpoint 任务，`/undo-task` 按任务边界回滚文件 | [core/checkpoint.md](core/checkpoint.md) |
| 工作区指纹 | manifest（文件清单）→ fingerprint（哈希）→ changes（两次快照 diff），replay 判漂移的依据 | [tools/audit.md](tools/audit.md) |
