# `tools/safety.py` 逐段讲解

> 本篇讲安全门（SafetyGate）。上游是 [tools/tools.md](tools.md) 的 `ToolRuntime._prepare()`（每次工具执行前问它）和 [core/loop.md](../../loop-explained.md)（构造它、把批准记忆存进 session）；下游是 [core/types.md](../core/types.md)（`FunctionCall` / `SafetyVerdict`）和 [core/queue.md](../core/queue.md)（`AskWaiter` 跨线程 y/N）。

一句话职责：**在工具真正执行之前**决定 allow / ask / deny，把"能不能做"从"怎么做"里彻底剥离出来。

- 行数：432 行
- 依赖：
  - [core/types.md](../core/types.md) —— `FunctionCall`、`SafetyVerdict`、`unique()`（保序去重）
  - [core/queue.md](../core/queue.md) —— `AskWaiter` / `TurnQueue.request_ask()`，工作线程里的 y/N 交接
  - [tools/workspace.md](workspace.md) —— `Workspace.resolve()` 是另一道（更硬的）越界防线，本模块只做**预判**
- 被谁用：
  - [tools/tools.md](tools.md) —— `ToolRuntime._prepare()` 调用 `safety.review()`，deny 直接转成 `ToolResult(blocked=True)`
  - [tools/audit.md](audit.md) —— 复用 `is_sensitive_path()` 决定事件流里哪些内容要脱敏
  - [core/loop.md](../../loop-explained.md) —— 第 77–78 行构造 `SafetyGate`，把 `session.approvals` 灌进 `memory`
  - [ui/app.md](../ui/app.md) —— `ask_yes_no()` 作为 `ask` 回调注入

## 目录

- [0. 速查表](#0-速查表)
- [1. 文档串与导入（1–17 行）](#1-文档串与导入1–17-行)
- [2. 常量层：白名单与危险清单（19–57 行）](#2-常量层白名单与危险清单19–57-行)
- [3. BashIntent：意图的中间表示（59–65 行）](#3-bashintent意图的中间表示59–65-行)
- [4. SafetyGate：三层裁决（67–102 行）](#4-safetygate三层裁决67–102-行)
- [5. approval_keys：批准指纹（105–115 行）](#5-approval_keys批准指纹105–115-行)
- [6. is_sensitive_path：敏感路径判定（117–133 行）](#6-is_sensitive_path敏感路径判定117–133-行)
- [7. classify：按工具名分流（135–155 行）](#7-classify按工具名分流135–155-行)
- [8. classify_bash：从意图到裁决（157–179 行）](#8-classify_bash从意图到裁决157–179-行)
- [9. parse_bash_intent：多段取最高风险（181–198 行）](#9-parse_bash_intent多段取最高风险181–198-行)
- [10. short_args 与 _is_destroy（200–223 行）](#10-short_args-与-_is_destroy200–223-行)
- [11. _segments：尊重引号的命令拆分（225–265 行）](#11-_segments尊重引号的命令拆分225–265-行)
- [12. _parse_segment：单段动词分类（267–302 行）](#12-_parse_segment单段动词分类267–302-行)
- [13. _strip_wrappers 与 _skip_mode（304–325 行）](#13-_strip_wrappers-与-_skip_mode304–325-行)
- [14. _parse_git / _parse_python / _parse_find（327–376 行）](#14-_parse_git--_parse_python--_parse_find327–376-行)
- [15. 路径收集与越界解析（378–432 行）](#15-路径收集与越界解析378–432-行)

---

## 0. 速查表

| 名字 | 行号 | 一句话职责 |
|---|---|---|
| `AskFn` | 20 | y/N 回调签名 `Callable[[str], bool]` |
| `READ_ONLY` | 23 | 纯只读工具集合，直接放行 |
| `OWN_JOBS` | 25 | 只作用于自己后台作业的工具集合 |
| `KEY_NAMES` | 27 | 敏感密钥/凭据文件名 |
| `ENV_ALLOW` | 29 | `.env` 的可公开变体（不敏感） |
| `WRAPPERS` | 31 | 无害前缀包装命令（剥掉后看真动词） |
| `ASK_ACTIONS` | 33–44 | 需要用户确认的动作 |
| `DENY_ACTIONS` | 46 | 直接拒绝的动作 |
| `PIPE_REMOTE` | 48 | `curl\|sh` 类远程脚本直执行 |
| `ASSIGN` | 50 | 环境变量赋值前缀 `FOO=bar` |
| `PY_DELETE` | 52–57 | Python 里显式删文件的调用（提取路径） |
| `BashIntent` | 59–65 | 解析结果：动作 + 路径 + 越界路径 |
| `SafetyGate` | 67–102 | 安全门主体：`review()` 出最终裁决 |
| `approval_keys()` | 105–115 | 生成"批准指纹"，供记忆复用 |
| `is_sensitive_path()` | 117–133 | 敏感路径判定（也被 audit 复用） |
| `classify()` | 135–155 | 纯规则分类，按工具名分流 |
| `classify_bash()` | 157–179 | bash 意图 → 裁决 |
| `parse_bash_intent()` | 181–198 | 拆段、逐段解析、取最高风险 |
| `short_args()` | 200–204 | 确认弹窗的参数摘要（截 240 字符） |
| `_is_destroy()` | 206–223 | 不可逆机器级破坏命令识别 |
| `_segments()` | 225–265 | 按 shell 分隔符拆子命令，尊重引号 |
| `_parse_segment()` | 267–302 | 单段命令的动词分类 |
| `_strip_wrappers()` | 304–314 | 剥掉赋值与包装前缀 |
| `_skip_mode()` | 316–325 | chmod/chown 跳过模式参数 |
| `_parse_git()` | 327–347 | 只拦 reset --hard / clean / push --force |
| `_parse_python()` | 349–361 | 检查 `python -c` 里的删除调用 |
| `_parse_find()` | 363–376 | `find -delete` / `-exec` 视为删除 |
| `_collect_paths()` | 378–382 | 从参数里抓路径（跳过选项） |
| `_resolve_many()` | 384–395 | 批量解析为（区内路径, 越界路径） |
| `_escapes()` | 397–402 | 单路径是否越界 |
| `_resolve_target()` | 404–432 | 路径解析核心：返回（相对路径, 是否越界） |

---

## 1. 文档串与导入（1–17 行）

文档串把三层结构说清楚了：

1. 只读工具直接放行；
2. `write`/`edit` 检查敏感路径与越界；
3. `bash` 解析意图并识别破坏性命令。

再加一句关键的补充：**ask 可被用户记忆（memory）中的历史批准覆盖**。

导入只有三个标准库（`json` / `re` / `shlex`）和一个自家模块（`core.types`）。`shlex` 是 bash 分词的关键——第 12 节展开。注意这里**不依赖** [tools/workspace.md](workspace.md)：本模块自己实现了一套路径解析（`_resolve_target`），只在 `SafetyGate.workspace` 里存一个 `Path`。这是有意的：安全门要能在没有 `Workspace` 实例的场合（单测、预检）独立跑。

---

## 2. 常量层：白名单与危险清单（19–57 行）

这一层是整套规则的"数据"，改动行为基本就是改这几个集合。

### READ_ONLY（22–23 行）

```python
READ_ONLY = {"read", "ls", "grep", "glob", "web_search", "web_fetch", "bash_poll"}
```

**为什么能直接放行**：这些工具的实现本身不写工作区，风险面只有"读到不该读的东西"。所以 `classify()` 对它们只做**一件事**——检查 `path` 参数是否越界（第 7 节）。越界就 deny，否则直接 allow，连 ask 都不走。

注意 `bash_poll` 在只读集合里：它只读后台作业的输出缓冲区，不改文件系统。

### OWN_JOBS（24–25 行）

```python
OWN_JOBS = {"bash_kill"}
```

`bash_kill` 只能杀**自己这次 run 启的**后台作业（[tools/tools.md](tools.md) 里 job_id 是本进程内的），杀不掉别人的进程，所以放行。它不进 `READ_ONLY` 是因为语义上它不是"读"，只是"作用域受限的写"。

### KEY_NAMES 与 ENV_ALLOW（26–29 行）

```python
KEY_NAMES = {"id_rsa", "id_ed25519", "id_ecdsa", "credentials.json", "auth.json", "secrets.json"}
ENV_ALLOW = {".env.example", ".env.sample"}
```

`KEY_NAMES` 是常见的 SSH 私钥名和凭据文件名——**名字本身就是语义**：叫 `id_rsa` 的文件不管在哪都当私钥处理。

`ENV_ALLOW` 是 `.env` 规则的例外表。`.env.example` / `.env.sample` 是仓库里给人抄的模板，里面全是占位符，没有真密钥，所以不该被拦——否则模型每次建示例配置都要被拒。

### WRAPPERS（30–31 行）

```python
WRAPPERS = {"time", "nohup", "command", "nice", "env", "busybox"}
```

这些命令只改变执行方式，不改变执行什么。`time rm -rf x` 和 `rm -rf x` 风险一样，所以解析时必须剥掉前缀，否则 `time rm` 会因为首词是 `time` 而漏判成 allow。这是**对抗性思维**：模型（或用户）完全可能无意识地写出 `nohup rm ...`。

### ASK_ACTIONS 与 DENY_ACTIONS（32–46 行）

```python
ASK_ACTIONS = {"delete", "move", "chmod", "chown", "git_reset", "git_clean",
               "git_force_push", "kill", "sql_drop", "crontab"}
DENY_ACTIONS = {"destroy", "privilege"}
```

两级，**不可逆程度**是分界线：

- `ASK_ACTIONS`：会造成损失但通常可恢复，或者作用域有限 → 问用户。
- `DENY_ACTIONS`：机器级破坏（`destroy`）或提权（`privilege`）→ 连问都不问，直接拒。理由是不该让用户承担"手滑按 y"的代价，而且 sudo 之后的命令已经超出 agent 的权限模型。

注意 `ASK_ACTIONS` 里的值是**归一化后的动作标签**，不是命令名：`rm`/`rmdir`/`unlink`/`shred` 统一映射成 `delete`，`python -c "os.remove(...)"` 和 `find -delete` 也映射成 `delete`。

### 三个正则（47–57 行）

```python
PIPE_REMOTE = re.compile(r"(?:curl|wget)\s+[^\n|]*\|\s*(?:sh|bash)")
ASSIGN      = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PY_DELETE   = re.compile(r"""(?:os\.(?:remove|unlink|rmdir)|shutil\.rmtree)\(\s*['"]([^'"]+)['"]"""
                         r"""|Path\(\s*['"]([^'"]+)['"]\s*\)\.unlink""")
```

- `PIPE_REMOTE`：`[^\n|]*` 保证不跨过管道符匹配——瞄的就是"下载后立即执行"这一个模式。这是供应链攻击的标准入口，拦得越死越好。
- `ASSIGN`：环境变量赋值前缀。`FOO=bar rm x` 的真动词是 `rm`。
- `PY_DELETE`：两个捕获组（函数式调用 / `Path(...).unlink()`），所以第 349 行取值时写 `[a or b for a, b in findall(...)]`——两组互斥，取非空的那个。

---

## 3. BashIntent：意图的中间表示（59–65 行）

```python
@dataclass
class BashIntent:
    action: str = "allow"
    paths: list[str] = field(default_factory=list)
    escaped: list[str] = field(default_factory=list)
```

解析和裁决之间的**中间层**。拆成两个动作是有价值的：`parse_bash_intent()` 只负责"看懂命令"，`classify_bash()` 只负责"定风险"。这样 `approval_keys()` 能复用解析结果生成指纹，而不用重新分类一遍。

`paths` 存的是**已解析的工作区内相对路径**，`escaped` 存越界的原始字面量——两者分开，因为裁决时给用户的理由用的是 `escaped[0]`（原始写法更好认），而记忆的 key 用的是 `paths`。

---

## 4. SafetyGate：三层裁决（67–102 行）

### 构造（74–84 行）

```python
self.interactive = interactive
self.ask = ask
self.memory: set[tuple[str, ...]] = memory if memory is not None else set()
self.workspace = Path(workspace).resolve() if workspace is not None else None
```

四个可注入点：能不能问人、问谁、已批准过什么、工作区在哪。`workspace` 在构造时就 `resolve()` 一次——后续每次 `_resolve_target()` 都拿它当基准，避免重复解析开销。

### `review()`（86–102 行）

**判定顺序**（这是全模块最重要的流程）：

```python
verdict = classify(call, self.workspace)          # ① 纯规则
if verdict.decision == "allow":
    return verdict
keys = approval_keys(call, self.workspace)        # ② 算批准指纹
if verdict.decision == "ask" and keys and all(key in self.memory for key in keys):
    return SafetyVerdict("allow", "remembered approval", source="memory")
if verdict.decision == "deny":
    return verdict                                # ③ deny 不受记忆影响
if verdict.decision == "ask":
    if not self.interactive:                      # ④ 非交互 → 降级为 deny
        return SafetyVerdict("deny", f"non-interactive: {verdict.reason}", source=verdict.source)
    prompt = f"{verdict.reason}\n{short_args(call)}\nAllow this action?"
    if self.ask and self.ask(prompt):             # ⑤ 问人
        self.memory.update(keys)                  #    批准后记住
        return SafetyVerdict("allow", "user approved", source="user")
    return SafetyVerdict("deny", "user declined or no prompt available", source="user")
```

四个设计点：

1. **allow 短路在最前**。绝大多数调用是只读或区内写，一次集合查询就返回，不走后面的指纹计算。
2. **`deny` 早于 `ask` 处理，且不受 memory 影响。**记忆只能把 ask 提升为 allow，**永远不能把 deny 降级**——否则用户批准过一次 `rm`，之后所有 `rm` 都放行了，这是不可逆的授权扩散。
3. **非交互时 ask 降级为 deny，不是升级为 allow。**`--json` 批处理、`wheel "任务"` 一次性模式下没人回答问题，默认拒绝是唯一安全的选择。理由里带上 `non-interactive:` 前缀，方便事后看出是模式导致而非规则禁止。
4. **`self.ask and ...` 的写法**：没有 `ask` 回调时（`ask=None`）等价于拒绝，同样落到 `user declined or no prompt available`。

返回的 `SafetyVerdict.source` 有四种取值：`rules` / `memory` / `user` / `verdict.source`。这个字段一路带到 [tools/tools.md](tools.md) 的 `ToolResult.safety_source`，再进 [tools/audit.md](audit.md) 的事件流——所以事后能从日志里区分"这条是规则放的"还是"人点的 y"。

### ask 如何跨线程工作

`review()` 是在**工作线程**里被 `ToolRuntime._prepare()` 调的（[tools/tools.md](tools.md) 用线程池并发跑工具）。直接在工作线程调 `input()` 会抢终端输入、藏掉 `>` 提示符。所以：

```
SafetyGate.review()                    # 工作线程
  → self.ask(prompt)                   # = ui/app.py 的 ask_yes_no()
      → queue.request_ask(prompt)      # 工作线程挂上 AskWaiter 并阻塞
          ← 主线程忙等循环轮询 queue.pending_ask()，拿到后弹窗、waiter.resolve()
      → 返回 bool
```

[core/queue.md](../core/queue.md) 的 `AskWaiter.wait()` 用 0.1 秒分段等待，每轮检查 `abort` 事件——用户按 `/stop` 时不再干等，直接按"拒绝"返回，工具线程不会被永久挂起。

---

## 5. `approval_keys`：批准指纹（105–115 行）

```python
def approval_keys(call, workspace=None) -> list[tuple[str, ...]]:
    if call.name != "bash":
        return [(call.name, json.dumps(call.arguments, sort_keys=True, ensure_ascii=False))]
    command = str(call.arguments.get("command") or call.arguments.get("cmd") or "").strip()
    intent = parse_bash_intent(command, workspace)
    if intent.action == "allow":
        return []
    paths = intent.paths or ["*"]
    return [("bash", intent.action, path) for path in paths]
```

**key 的两种形状**：

- 非 bash 工具：`(工具名, 规范化的参数 JSON)`。`sort_keys=True` 保证字段顺序不影响哈希，`ensure_ascii=False` 保证中文路径稳定。这是最严格的粒度——参数变一个字符就不命中。
- bash：`("bash", 动作, 相对路径)`，每条路径一个 key。粒度放宽到"动作 + 目标"，所以 `rm -rf build/` 批准之后，`rm -rf build/` 再调一次直接放行，但 `rm -rf src/` 仍然要问。

**为什么能省掉重复确认**：agent 改代码时常反复执行同一条命令（跑测试前清构建产物、重装依赖）。同一个 run 内 `self.memory` 累积，跨 run 则由 [core/loop.md](../../loop-explained.md) 第 250 / 362 行把 `safety.memory` 写回 `session.approvals`，下次启动再灌回 `memory`（第 77 行）。**用户只需要回答一次。**

两个细节：

- `intent.action == "allow"` 时返回空列表——没有风险就不需要记忆。
- `paths` 为空时用 `["*"]` 兜底。典型场景是 `git reset --hard`、`git clean`（`_parse_git` 返回 `["."]`，不为空）和 `kill`（无路径）。`kill` 走的就是 `("bash", "kill", "*")` 这种通配 key——批准一次，之后所有 `kill` 都放行，因为杀进程的作用域本来就受限。

`review()` 里用的是 `all(key in self.memory for key in keys)`：**所有** key 都命中才放行。一条命令涉及多个路径时，只批准过其中一个是不能放行的。

---

## 6. `is_sensitive_path`：敏感路径判定（117–133 行）

```python
posix = path.replace("\\", "/").strip()
parts = [p for p in posix.split("/") if p and p not in {".", ".."}]
if ".git" in parts:
    return True
name = parts[-1] if parts else posix.rsplit("/", 1)[-1]
if name in KEY_NAMES: return True
if name.endswith((".pem", ".p12", ".pfx", ".key")): return True
if name == ".env": return True
if name.startswith(".env.") and name not in ENV_ALLOW: return True
return False
```

三条判定，从粗到细：

1. **路径任一段是 `.git`** → 敏感。整个 `.git` 目录都是禁区：里面有 objects、hooks（写 hooks 等于拿到代码执行）、config（可能含凭据）。
2. **文件名在 `KEY_NAMES`** 或 **扩展名是证书/私钥类**（`.pem` / `.p12` / `.pfx` / `.key`）。注意扩展名规则是**任意目录**生效——`tmp/foo.key` 也算。
3. **`.env` 及 `.env.*`**，但 `ENV_ALLOW` 里的两个例外先剔除。

先把反斜杠统一成正斜杠，再过滤掉 `.` 和 `..` 段——这样 `a/./b/../id_rsa` 会被正确识别为 `id_rsa`，不会因为在路径中间就漏判。

这个函数**被三处复用**，不止安全门：

- 本模块 `classify()` 第 144 行：`write`/`edit` 写敏感路径 → deny。
- 本模块 `classify_bash()` 第 173 行：ask 级动作命中敏感路径 → 升级为 deny。
- [tools/audit.md](audit.md) 第 123 / 132 行：`redact_tool_args()` / `redact_tool_output()`。写敏感路径时把 `content` 换成 `<redacted>`，读敏感路径时整个输出替换成 `<redacted sensitive tool output>`。

第三处是**防御纵深**：就算安全门被绕过（比如路径判定漏了什么），密钥内容也不会落进 `events.jsonl`。

---

## 7. `classify`：按工具名分流（135–155 行）

纯函数，不含交互和记忆——所以可以单独测试、也可以在 `review()` 之外调用。

```python
if call.name in READ_ONLY:
    path = str(call.arguments.get("path") or "")
    if path and _escapes(path, workspace):
        return SafetyVerdict("deny", f"path escapes workspace: {path}", source="rules")
    return SafetyVerdict("allow", "read-only tool", source="rules")
```

只读工具唯一的风险是**越界读**（`/etc/passwd`、上层目录的 `.env`），所以只查 `_escapes()`。`path` 为空串时不查（比如 `bash_poll` 传的是 `job_id`，`ls` 不传 path）。

```python
if call.name in {"write", "edit"}:
    if is_sensitive_path(path): return deny(f"refusing to modify sensitive path {path}")
    if _escapes(path, workspace):      return deny(f"path escapes workspace: {path}")
    return allow("workspace mutation is allowed")
```

`write`/`edit` 的**敏感检查在越界检查之前**。顺序有意义：敏感路径判断不依赖 workspace，先跑能更早拒掉；而且给用户的理由里"敏感"比"越界"更准确。

**区内改文件默认是 allow**——这是 coding agent 的核心能力，每写一次文件都弹窗就没法用了。安全边界靠两层保证：越界拦截（本模块）+ [tools/workspace.md](workspace.md) 的 `resolve()` 再拦一次 + [core/checkpoint.md](../core/checkpoint.md) 的 undo 快照。

```python
if call.name in {"plan", "harness"} | OWN_JOBS:
    return SafetyVerdict("allow", "workspace mutation is allowed", source="rules")
```

`plan` 和 `harness` 也在这里放行。`plan` 实际上是**自带交互的**——它内部用 `PlanStore(ask=safety.ask, ...)`（[tools/tools.md](tools.md) 第 266 行）弹自己的确认框，安全门再拦一次就是重复。`harness` 只改自己的笔记文件（[harness/harness.md](../harness/harness.md)），不在用户代码区。

兜底：

```python
if call.name == "bash": return classify_bash(command, workspace)
return SafetyVerdict("ask", f"unknown tool {call.name}", source="rules")
```

**未知工具一律 ask，不是 deny。**注册新工具后忘了更新安全门时，用户会看到一个"unknown tool"的确认框，功能仍然可用（比直接拒绝好），同时明确提示了遗漏。这是 fail-safe 但不是 fail-closed——对本地 agent 是合理的取舍。

---

## 8. `classify_bash`：从意图到裁决（157–179 行）

```python
text = command.strip()
if not text:                        return deny("empty command")
if _is_destroy(text):               return deny("destructive machine command")
if re.search(r"\bsudo\b", text):    return deny("sudo escalates privileges")
intent = parse_bash_intent(text, workspace)
if intent.action in DENY_ACTIONS:   return deny(f"{intent.action} is not allowed")
if intent.escaped:                  return deny(f"path escapes workspace: {intent.escaped[0]}")
if intent.action in ASK_ACTIONS:
    sensitive = [p for p in intent.paths if is_sensitive_path(p)]
    if sensitive:                   return deny(f"refusing to {intent.action} sensitive path {sensitive[0]}")
    target = ", ".join(intent.paths) if intent.paths else "workspace"
    return ask(f"{intent.action} {target}")
return allow("command not on danger list")
```

**六道闸，顺序即从最硬到最软**：

1. 空命令 → deny（模型偶尔会输出空串）。
2. `_is_destroy()` → deny。整条命令扫描，不分段——因为 `curl|sh` 这类是"整条管道"的危险。
3. `sudo` → deny。同样整条扫描（`\b` 词边界，避免匹配到路径里的 `sudo`）。
4. 意图是 `destroy` / `privilege` → deny。注意这一步和第 2、3 步**有重叠**：`_is_destroy()` 只看整串正则，`sudo` 只看关键词，而 `parse_bash_intent()` 能识别出被 `&&` 连在后面的 `sudo`（分段后首词命中）。三层覆盖不同的漏判路径。
5. 任一路径越界 → deny。这是**硬边界**：`rm ../../etc/foo` 不是"问一问"能解决的。
6. ask 级动作 → **先看是否命中敏感路径，命中则升级为 deny**（第 6 节的第二处复用）；否则 ask，理由里带上具体目标（`delete build, dist`），让用户知道在批准什么。

`target = ... or "workspace"`：无路径时（如 `kill`）提示作用于工作区，理由不会是空串。

最后的 `allow("command not on danger list")` 是**黑名单模型**——不在危险清单上的都放行。对 coding agent 这是必要的：`git status`、`pytest`、`pip install` 这类命令无穷无尽，白名单根本列不完。代价见第 16 节。

---

## 9. `parse_bash_intent`：多段取最高风险（181–198 行）

```python
for segment in _segments(command):
    piece = _parse_segment(segment, workspace)
    actions.append(piece.action); paths.extend(piece.paths); escaped.extend(piece.escaped)
action = "allow"
for candidate in ("destroy", "privilege", "delete", "move", "git_reset", "git_clean",
                  "git_force_push", "chmod", "chown", "kill", "sql_drop", "crontab"):
    if candidate in actions:
        action = candidate
        break
```

两个要点：

**拆段**：`cd /tmp && rm -rf x` 是两段，第一段 `cd /tmp` 解析为 allow，第二段是 delete。不拆段就会因为首词是 `cd` 而整体漏判。

**取最高风险**：多段命令的整体风险 = 各段风险的**最大值**。这个候选列表是手写的风险降序，不用 `ASK_ACTIONS` 的排序是因为集合无序——必须显式定序。列表里 `destroy` 和 `privilege`（deny 级）排在 ask 级前面，保证 deny 优先。

**取舍**：只保留"最高"那一个动作，`paths` 和 `escaped` 却是所有段的并集。所以 `rm a.txt && mv b c` 的裁决是 `delete a.txt`（delete 排在 move 前面），但 `mv c` 的路径不会出现在提示里——用户看到的理由不完整。这是简化：完整的多动作裁决需要多条确认，对本地 agent 来说过重。`unique()`（[core/types.md](../core/types.md)）保证路径列表保序去重。

---

## 10. `short_args` 与 `_is_destroy`（200–223 行）

### `short_args()`（200–204 行）

```python
raw = json.dumps(call.arguments, ensure_ascii=False)
return raw if len(raw) <= 240 else raw[:237] + "..."
```

确认弹窗里的参数摘要。240 字符是"一屏能看完"的取舍——[harness/harness.md](../harness/harness.md) 里笔记内容上限也是 240 字符，同一量级的 UI 约束。截断到 237 + `...`，保持总长仍是 240。

### `_is_destroy()`（206–223 行）

七条不可逆破坏的识别：

| 检查 | 行号 | 拦什么 |
|---|---|---|
| `PIPE_REMOTE.search(text)` | 208 | `curl ... \| sh` 供应链攻击 |
| `re.search(r":\(\)\s*\{", text)` | 210 | fork 炸弹 `:\(){ :\|:& };:` |
| `mkfs` / `wipefs` | 212 | 格式化磁盘 |
| `reboot` / `shutdown` / `poweroff` / `halt` | 214 | 关机重启 |
| `>\s*/dev/` | 216 | 直接写块设备 |
| `/etc/passwd` / `/etc/shadow` | 218 | 动系统账密 |
| `\bdd\b` 且含 `/dev/` | 220 | `dd` 写设备 |

最后一条是**合取条件**：带 `/dev/` 的 `dd` 才危险，`dd if=x of=y`（普通文件拷贝）不该拦。同理第 216 行的 `>` 重定向需要紧跟 `/dev/`。

这一层全部是**整条命令的正则**，不做分词。理由：这些破坏模式的关键在于"管道/重定向 + 目标"，分词反而会丢失结构信息。

---

## 11. `_segments`：尊重引号的命令拆分（225–265 行）

手写的字符扫描器，按 `;` / `&&` / `||` / `|` / 换行拆段。

为什么不用 `shlex.split()` 直接拆：因为要**保留分隔符的语义位置**——只在分隔符处切断，引号内的分隔符不算数。

```python
while i < n:
    ch = command[i]
    if quote:                                     # ① 在引号内：只找闭合引号
        buf.append(ch)
        if ch == quote: quote = ""
        i += 1
        continue
    if ch in {"'", '"'}:                          # ② 进入引号
        quote = ch; buf.append(ch); i += 1
        continue
    if command.startswith("&&", i) or command.startswith("||", i):   # ③ 双字符分隔符
        piece = "".join(buf).strip()
        if piece: parts.append(piece)
        buf = []; i += 2
        continue
    if ch in ";\n" or (ch == "|" and not command.startswith("||", i)):  # ④ 单字符分隔符
        ...
```

两个细节：

- **先查 `&&`/`||` 再查 `|`**（③ 在 ④ 前），否则 `a && b` 会被 `|` 分支误切成 `a &` 和 `& b`。④ 里额外判 `not startswith("||", i)` 也是同一目的。
- **引号字符本身留在 buf 里**（`buf.append(ch)`）。切出的段是 `rm "my file.txt"` 这样带引号的完整串，下一步 `shlex.split()` 才能正确把它解析成一个 token。

最后收尾 `piece = "".join(buf).strip()` 处理没有结尾分隔符的情况（`rm -rf x` 只有一段）。

---

## 12. `_parse_segment`：单段动词分类（267–302 行）

```python
try:
    tokens = shlex.split(segment, posix=True)
except ValueError:
    tokens = segment.split()
tokens = _strip_wrappers(tokens)
```

用 `shlex.split(posix=True)` 做标准 shell 分词（处理引号、转义）。`ValueError` 兜底是给**未闭合引号**的（`rm "unterminated`）——这时退化为朴素 `split()`，宁可解析粗糙也别抛异常中断整个流程。

然后按动词分派：

| 动词 | 行号 | 动作 | 路径来源 |
|---|---|---|---|
| `rm` / `rmdir` / `unlink` / `shred` | 271–273 | `delete` | `_collect_paths(args)` |
| `mv` | 274–276 | `move` | `_collect_paths(args)` |
| `chmod` / `chown` | 277–279 | 同名 | `_collect_paths(_skip_mode(args))` |
| `git` | 280–281 | —— | `_parse_git(args)` |
| `python` / `python3` | 282–283 | —— | `_parse_python(args)` |
| `find` | 284–285 | —— | `_parse_find(args)` |
| `kill` / `killall` | 286–287 | `kill`（无路径） | —— |
| `crontab` | 288–289 | `crontab`（无路径） | —— |
| （整段匹配 `drop table/database`） | 290–291 | `sql_drop` | —— |
| 其他 | 292 | `allow` | —— |

`verb = Path(tokens[0]).name`（269 行）：取命令名的最后一段，`/bin/rm` 和 `rm` 等价。

`sudo` 的处理在剥包装**之后**（270 行）单独判——因为 `_strip_wrappers` 不会剥 `sudo`，它要被识别成 `privilege` 动作而不是被忽略。

`sql_drop` 用整段正则 + `re.I`（290 行）：SQL 可能出现在 `psql -c "..."` 或 heredoc 里，分词拿不到"命令名"，只能整段扫。

---

## 13. `_strip_wrappers` 与 `_skip_mode`（304–325 行）

### `_strip_wrappers()`（304–314 行）

```python
out = list(tokens)
while out and ASSIGN.match(out[0]): out.pop(0)          # 剥 FOO=bar
while out and out[0] in WRAPPERS:                        # 剥 time/nohup/...
    out.pop(0)
    while out and ASSIGN.match(out[0]): out.pop(0)      # 包装后再剥一次赋值
```

**双层循环 + 内层重复**：`time FOO=bar rm x` 这种"包装命令和赋值交替"的写法，剥掉 `time` 后还得再剥 `FOO=bar`。内层 `while` 处理这一层。

### `_skip_mode()`（316–325 行）

`chmod` / `chown` 的第一个参数是模式或属主，不是路径，必须跳过否则会把 `755` 当成文件名：

```python
if not args: return args
if args[0].startswith("-") and len(args) > 1: return args[1:]     # -R 之类的选项
if re.match(r"^[0-7]{3,4}$", args[0]) or ":" in args[0] \
   or args[0].startswith("u") or args[0].startswith("g"): return args[1:]
return args
```

四条判定覆盖：`chmod 755 f`（八进制，3–4 位）、`chown user:group f`（冒号）、`chmod u+x f`（`u`/`g` 开头的符号模式）。

`len(args) > 1` 的保护：选项后面没参数时不越界。`[0-7]{3,4}` 而不是 `{3}` 是因为 `2755`（setuid）也是合法的。

这个"猜第一个参数"的做法不严谨（`chmod 755` 后面跟文件名 `755` 会被误跳），但宁可**少抓一个路径**（退化成 ask 而不是 deny，且 target 显示 `workspace`）也不要把模式当路径。

---

## 14. `_parse_git` / `_parse_python` / `_parse_find`（327–376 行）

### `_parse_git()`（327–347 行）

先用一段循环跳过全局选项，定位真正的子命令：

```python
while i < len(args):
    if args[i] == "-C" and i + 1 < len(args): i += 2; continue   # -C <path> 带参数
    if args[i].startswith("-") and args[i] not in {"-"}: i += 1; continue
    break
```

`-C` 特殊处理是因为它**带一个参数**，直接 `i += 1` 会把路径当成下一个选项。

只拦三个子命令，路径统一填 `["."]`（整个工作区）：

| 子命令 | 条件 | 动作 |
|---|---|---|
| `reset` | 参数含 `--hard`（或 `--hard=...`、`--merge`） | `git_reset` |
| `clean` | 无条件 | `git_clean` |
| `push` | 参数含 `force` | `git_force_push` |

其他 git 操作（`commit`、`checkout`、`status`、`branch`）全部放行。**这是刻意的宽松**：`git` 本身有 reflog 和对象库兜底，大部分操作可恢复，而且 coding agent 需要频繁用 git。真正不可恢复的只有这三个。

`push --force` 也在这个级别：它破坏的是**远端共享历史**，影响别人，所以值得问一次。

### `_parse_python()`（349–361 行）

```python
blob = " ".join(args)
if "-c" in args:
    idx = args.index("-c")
    if idx + 1 < len(args): blob = args[idx + 1]
if not re.search(r"os\.(?:remove|unlink|rmdir)|shutil\.rmtree|\.unlink\s*\(", blob):
    return BashIntent()
found = [a or b for a, b in PY_DELETE.findall(blob)]
paths, escaped = _resolve_many(found, workspace)
return BashIntent("delete", paths, escaped)
```

对 `python -c` 单独取 `-c` 的参数字符串（否则 `blob` 里混着其他命令行参数，可能误匹配）。

**先粗筛再精提**：`re.search()` 快速否定掉绝大多数 `python` 调用（跑脚本、起服务都不含删除调用），只有命中才跑 `PY_DELETE.findall()` 提取具体路径。

只认**字面量路径**。`os.remove(path_var)` 提取不到路径 → `paths` 为空 → key 退化成 `("bash", "delete", "*")`，裁决仍然是 ask（动作命中 `ASK_ACTIONS`），只是提示里的目标显示 `workspace`。

### `_parse_find()`（363–376 行）

```python
if not any(item in {"-delete", "-exec"} or item.startswith("-exec") for item in args):
    return BashIntent()
raw = []
for item in args:
    if item.startswith("-"): break      # 遇到第一个选项就停
    raw.append(item)
if not raw: raw = ["."]
```

`find -name '*.pyc' -delete` 是删除意图；`find . -name foo` 不是。取路径的方式是"第一个 `-` 开头的参数之前全是路径"，匹配 `find` 的语法（路径必须在表达式之前）。没取到就默认 `["."]`——`find -delete` 从当前目录删，语义正确。

---

## 15. 路径收集与越界解析（378–432 行）

### `_collect_paths()`（378–382 行）

```python
raw = [item for item in args if item != "--" and not item.startswith("-")]
return _resolve_many(raw, workspace)
```

从参数里抓路径的规则极简：**不以 `-` 开头的就是路径**，`--` 单独剔除（它是选项结束标记）。所以 `rm -rf -- foo` 能正确拿到 `foo`，`rm -rf` 拿到空列表。

### `_resolve_many()`（384–395 行）

批量解析，分成两个桶：

```python
for item in raw:
    rel, outside = _resolve_target(item, workspace)
    if outside:            escaped.append(item)      # 越界：存原始字面量
    elif rel is not None:  paths.append(rel)         # 区内：存相对路径
```

`escaped` 存原始字面量（提示里 `path escapes workspace: ../../etc/passwd` 比解析后的绝对路径更好认），`paths` 存相对路径（记忆 key 用它，保证换台机器、换个绝对路径也能命中）。

### `_escapes()`（397–402 行）

单路径的越界查询，被 `classify()` 用于只读工具和 `write`/`edit`。空路径返回 `False`——没有路径就不存在越界。

### `_resolve_target()`（404–432 行）

全模块的路径判定核心，两个分支：

**workspace 为 None 时**（413–420 行）：只能做字符串层面的判断。绝对路径（`/...`）、`~`、`..`、Windows 盘符（`posix[1] == ":"`）一律算越界；剩下剥掉前导 `./` 当区内路径。这是给"没配工作区"的降级路径。

**workspace 存在时**（421–432 行）：

```python
root = Path(workspace).resolve()
try:
    candidate = Path(text)
    candidate = candidate.resolve() if candidate.is_absolute() else (root / text).resolve()
except OSError:
    return posix.lstrip("./") or ".", False
try:
    rel = candidate.relative_to(root)
except ValueError:
    return None, True          # 越界
mapped = rel.as_posix()
return mapped if mapped != "." else ".", False
```

关键在 `.resolve()`：**先展开符号链接和 `..` 再判断**，所以 `workspace/link -> /etc` 这种符号链接逃逸能被抓住。不用 `resolve()` 的话 `a/../b` 的字符串比较会漏判（同层目录名冲突时）。

`text.startswith("$")` 直接返回 `(None, False)`（411 行）——**shell 变量不解析**。`rm $TARGET` 不知道要删哪，既不能判越界（可能合法），也不能提取路径，所以放弃判定：不作为越界（避免误拒），也不产生路径（不进记忆 key）。这类命令会落到 ask 或 allow，最终由 [tools/tools.md](tools.md) 的实际执行和 [core/checkpoint.md](../core/checkpoint.md) 的 undo 兜底。

`OSError` 兜底（423–424 行）：路径含 NUL 字节或超长时 `resolve()` 会抛异常，降级为"区内路径"而不是崩溃——宁可放行让工作区再拦一次。

注意 `mapped != "."` 的处理（431 行）：`rm .` 解析出相对路径是 `.`，保留这个字面值而不是变成空串，提示里显示更清楚。

---
