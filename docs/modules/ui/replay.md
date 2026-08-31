# `ui/replay.py` 逐段讲解

> 本篇讲回放机制。上游是 [ui/app/commands.py](app-commands.md) 的 `/replay` 命令和评测脚本，下游是 [core/loop.py](../../loop-explained.md) 的 `run_agent`（重跑走完全相同的代码路径）和 [core/events.py](../core/events.md) 的落盘数据。

一句话职责：用录制的模型响应替换真模型重跑一次 run，和原运行对比，给出 exact / behavioral / drift / error 四种状态。

- 行数：210 行
- 依赖：
  - [core/events.py](../core/events.md) —— `load_run` / `load_events` / `load_responses`（读录制数据）
  - [core/model.py](../core/model.md) —— `ScriptedModel`（模型替身，926 行起）
  - [core/loop.py](../../loop-explained.md) —— `run_agent`（重跑入口）
  - [ui/graph.py](graph.md) —— `list_session_runs`（按 session 列 run）
  - [core/config.py](../core/config.md) —— 构造假 `AgentConfig`
- 被谁用：[ui/app/commands.py](app-commands.md)（`/replay`）、评测脚本（session 级重放）

## 目录

- [1. `print_timeline`：事件流压成时间线](#1-print_timeline事件流压成时间线-22-57-行)
- [2. `recorded_scripts` 与事件筛选](#2-recorded_scripts-与事件筛选-60-90-行)
- [3. `_tool_signature` 与 `_input_signature`](#3-_tool_signature-与-_input_signature-70-90-行)
- [4. `_replay_status`：四态判定](#4-_replay_status四态判定-92-120-行)
- [5. `replay_run`：单 run 重跑](#5-replay_run单-run-重跑-123-163-行)
- [6. `copy_workspace`：干净工作区](#6-copy_workspace干净工作区-166-188-行)
- [7. `replay_session`：整 session 重放](#7-replay_session整-session-重放-191-210-行)

---

## 1. `print_timeline`：事件流压成时间线（22–57 行）

把 `events.jsonl` 压成人类可读文本，事件类型到行的映射：

| 事件 | 渲染成 |
|---|---|
| `agent_start` | `# <run_id>  <provider>/<model>` + `task: ...` |
| `turn_start` | `== turn N ==` |
| `message_end` | `[think]` 段 + 正文（都为空则不输出） |
| `tool_execution_start` | `→ <tool_name> <args JSON>` |
| `tool_execution_end` | `← ok/ERR/BLOCK <结果预览，>160 字符截成 157+...>` |
| `agent_end` | `stop=... turns=... tokens=输入+输出` |
| `error` | `ERROR <message>` |

只读事件、不发 API。`message_end` 的 `hide_text` 不在这层过滤——时间线是审计视图，plan 工具的回显文本也要能看到。

## 2. `recorded_scripts` 与事件筛选（60–68 行）

```python
def recorded_scripts(bus):
    return [row["output"] for row in bus.load_responses()]
```

从 `responses.jsonl` 取每轮录制的**模型原始输出**，顺序 = 调用顺序。这就是 `ScriptedModel` 的脚本：第 N 次 `complete()` 返回第 N 个录制输出。

`_events(bus, kind)` 是按 `type` 字段筛事件的单行 helper。

## 3. `_tool_signature` 与 `_input_signature`（70–90 行）

两个签名序列，是「两次运行行为是否一致」的判据：

**工具签名**（70–85 行）：按 index 把 `tool_execution_start` 和 `tool_execution_end` 配对（start[i] ↔ end[i]，不靠 call_id），每条取 `name` + `args`（脱敏后的）+ 结束事件的 `safety_decision`。序列相等 = 两次运行**调了同样的工具、同样的参数、同样的安全裁决**。

**输入签名**（87–90 行）：每轮 `responses.jsonl` 行的 `input_audit`（[core/loop.py](../../loop-explained.md) 第 5 节每轮拍的上下文指纹）。序列相等 = 两次运行**每轮喂给模型的上下文一致**。

## 4. `_replay_status`：四态判定（92–120 行）

```python
if target.stop_reason in {"error", "api_error"} or not replay_end:
    return "error", {...}
```

判定优先级（先判的先赢）：

1. **error**：重跑没走到 `agent_end`（抛异常或 API 错）。
2. **drift**：两端工作区指纹都在但**不同**——即使工具序列、输入序列全同也算漂移。注释点破原因：环境差异（依赖版本、系统库）可能让同样的调用产生不同的输出，指纹不同就是证据。
3. **exact**：工具签名 == 输入签名 == stop_reason 全同。
4. **behavioral**：其余情况——跑完了、指纹一致，但工具或输入有差异。这个状态不判「对错」，只标记「行为层面有分叉」，留给调用方看 `details`。

一个宽松点：`inputs_same` 在**任一边的输入签名为空**时判 True（`not any(source_inputs) or not any(replay_inputs)`）——老版本录制没有 `input_audit` 字段时不误报，对比退化成只看工具序列。

`details` 里带 `tools_same` / `inputs_same` / `stop_same` 和两端指纹，UI 展示哪一项不一致。

## 5. `replay_run`：单 run 重跑（123–163 行）

流程五步：

1. `load_run(runs_dir, run_id)` 找回原运行（[core/events.py](../core/events.md) 的三级查找：精确 ID → 前缀 → session_id）。
2. 从 `meta.json` 还原任务参数：`task`、`provider`、`base_url`、`model`、`turns`。
3. 构造**假 provider**：`api_key=""`、模型名原样保留（注释：录制的响应不用真 key）；`max_turns = 原 turn 数 + 2`（留余量，ScriptedModel 脚本耗尽会报错，+2 是给收尾紧凑留的调用空间）。
4. `run_agent(task, workspace, config, model=ScriptedModel(scripts), extra_meta={"replay_of": run_id, "mode": "replay"})`——**和正常 run 完全相同的入口**，只是模型是替身。所以 replay 的 run 也落盘自己的 `events.jsonl` / `responses.jsonl` / `meta.json`，可再被对比。
5. 对比重跑后的事件流，把 `replay_status` / `replay_details` 写回 `RunResult`（这两个字段见 [core/types.py](../core/types.md) 第 8 节）。

返回 `(timeline, result)`：时间线是给人看的，`result` 是给程序用的。

## 6. `copy_workspace`：干净工作区（166–188 行）

```python
_COPY_SKIP = {".wheel", ".wheel_runs", ".git", ".venv", "__pycache__", "node_modules"}
```

把源工作区拷到 dest（先 `rmtree` 旧的），两处刻意设计：

- **跳过产物目录**：`.git` / `.venv` / `node_modules` 等——它们既不进工作区指纹（[tools/audit.py](../tools/audit.md) 的 manifest 层跳过），又会让拷贝很慢。
- **符号链接原样保留**（`target.symlink_to(os.readlink(child))`）：`shutil.copy2` 会跟随链接，把 `link -> .env` 变成真拷贝 `.env`。`.env` 是敏感路径，进 manifest 的内容指纹会变，重放工作区的指纹就和录制的对不上——symlink 必须保持「指向」语义。

## 7. `replay_session`：整 session 重放（191–210 行）

1. `list_session_runs(session_id, runs_dir)`（[ui/graph.py](graph.md) 629 行起）按 session 列出全部 run，顺序执行。
2. 传了 `source_workspace` 就先 `copy_workspace` 铺一份干净工作区；不传就原地用。
3. 逐个 `replay_run`，`interactive=False`（回放不弹 y/N 询问，安全门非交互模式直接拒绝 ask）。
4. 返回 `RunResult` 列表，调用方聚合各 run 的 `replay_status`。

整个 session 是**顺序**跑的——后面的 run 依赖前面 run 留下的工作区状态和上下文（`items` 续接），并发会乱。
