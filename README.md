# Wheel Agent

> Because we're reinventing the agent wheel, we named it Wheel Agent.
>
> 因为是重复造 Agent 轮子，所以就叫 Wheel Agent。

A minimal but **complete** Python coding agent. One local agent loop, any
OpenAI-compatible endpoint (Responses or Chat Completions), a crash-safe
branching session store, prompt-cache-aware compaction, file-level undo, a
continual-learning harness, and replayable evaluations (Aider Polyglot with no
Docker; SWE-bench Lite through the official harness).

The design goal is not a leaderboard. The goal is that **every part of an
agent harness is visible and readable here**: ~12k lines of Python, runtime
dependencies limited to the `openai` SDK + `python-dotenv` (pytest for dev,
pexpect for the PTY suite), no framework, no MCP, no RPC. Just a loop, a file,
and a terminal. If you want to understand how a coding agent works end to
end — loop, streaming, cache, compaction, safety, undo, replay — this
is the codebase to read.

---

## 1. Quickstart

```bash
git clone <this repo> && cd wheel-agent
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # fill in API key + base_url
# optional: put the CLI on PATH (note the name, see below)
ln -s "$PWD/bin/wheel" ~/.local/bin/wheel
```

```bash
wheel                      # interactive REPL, workspace = current directory
wheel "fix this bug"       # one-shot task in the REPL
wheel --json "fix this bug"  # single JSON line on stdout, exit code is the verdict
python3 main.py "fix this off-by-one"   # same thing without the install
```

> **Heads-up: the command is named `wheel`.** pip ships a utility called
> `wheel` too; installing this package puts this agent first on PATH in your
> venv. If you rely on `wheel` (the pip utility), use `python3 -m wheel` or
> `bin/wheel` explicitly. The entry point is declared in `pyproject.toml`
> (`[project.scripts]`) and can be renamed there.

First minutes in the REPL:

```
> write hello.py in this directory and run it
> /tree            # the session tree; /tree <id> jumps & continues from there
> /refine          # extract durable lessons from this transcript into the harness
> /undo            # revert the last write/edit (no git required)
> /jobs            # background bash jobs
> effort high      # reasoning effort (bare word == /effort)
> quit
```

While a task runs: **Enter with text = steer** (the text joins the next model
call); **`/follow text`** = deliver after the run stops; **`Ctrl+C` or
`/stop`** = abort (completed turns stay in the session).

## 2. CLI reference

### `wheel`

```
wheel [--json|-j] [task...]
```

- The workspace is always the current directory — there is deliberately no
  `--workspace` flag; `cd` first.
- No task → interactive REPL. Task → run it, print the result, exit.
- `--json` prints exactly one JSON line and nothing else (streaming frames go
  to the TTY only in interactive mode):

```json
{"text": "...final answer...", "stop_reason": "stop", "run_id": "r1a2b3...",
 "task_id": "t001", "session_id": "s4f8...", "usage": {"input_tokens": 12345,
 "output_tokens": 234, "cached_tokens": 9876, "cache_write_tokens": 100,
 "reasoning_tokens": 0}, "changed_files": ["src/main.py"]}
```

  `stop_reason` ∈ `stop` / `max_turns` / `plan_rejected` / `error` /
  `api_error` / `aborted`. Exit code: `0` for stop/max_turns/plan_rejected,
  `1` otherwise, `2` for config errors (e.g. missing API key — that also
  prints a `{"error": ..., "stop_reason": "error"}` line). Cost is not in the
  payload; the meter computes it from the price vars in §4.

## 3. REPL reference

### Slash commands (the full list, aliases included)

| Command | What it does |
|---|---|
| `/help` | this reference (also `/`) |
| `/quit` `/exit` `/q` | exit (Ctrl+C / Ctrl+D too) |
| `/provider [name]` | switch model channel (↑↓ picker with no arg) |
| `/effort [level]` `/think [level]` | reasoning effort for the current model (↑↓ picker); bare `effort high` works from the prompt |
| `/compact` | compact the session now (summarize prefix, bump cache epoch) |
| `/undo [n]` | revert the last write/edit (or n of them) |
| `/undo-task` | rollback all file changes of the last task |
| `/new` | start a fresh session |
| `/sessions` | list sessions for this workspace |
| `/resume [id]` | resume a session (↑↓ picker with previews); continues from its tip |
| `/tree [id]` | session tree; ↑↓ + Enter jumps to a node and continues from there (this is how you fork) |
| `/fork [id]` | alias of `/tree` |
| `/plan` | print the current plan (see `plan` tool below) |
| `/harness` | show the continual harness (notes/memories that enter future prompts) |
| `/refine` | one manual refine: a cheap second model pass over the transcript proposes harness edits (CAS-checked) |
| `/refine auto [N\|off]` | background auto-refine every N user turns (default 8; also `WHEEL_AUTO_REFINE` env) |
| `/refine rollback <id>` | revert a previous refine's edits |
| `/jobs` `/jobs kill [id]` | list / kill background bash jobs |
| `/graph` `/graph html` | the turn/tool DAG of the current path, as ascii or an HTML page (local http server for the html) |
| `/replay [run_id] [go]` | browse recorded runs / replay a run against its recorded responses |
| `/replay session [dir]` | replay every run of this session in order |
| `/follow <text>` | queue text to be delivered as a user turn after the current run stops |
| `/stop` | abort the current run (Ctrl+C equivalent) |
| `/expand r12` | expand a clipped tool output (the clip shows the run id) |
| `/max-turns [n]` | show/set the turn cap (0 = unlimited) |

Every command also works as a bare word (`quit`, `tree`, `jobs`) — the
dispatch table is the same one that feeds the slash menu.

### The input editor

- Full line editing (arrows, Home/End, Ctrl+A/E/W/K, delete, word jump),
  multi-line visual wrap, history (↑ at bottom keeps the in-progress line —
  standard readline semantics).
- **Bracketed paste**: multi-line pastes arrive as one action; the menu and
  shortcuts stay quiet during a paste.
- **Slash menu**: type `/` and matches list with usage; ↑↓ + Enter accepts,
  Esc dismisses; the menu never eats vertical space you don't have.
- **@file completion**: `@` lists files in the workspace (also feeds the
  agent: `@src/main.py` in a task expands to the file's content).
- **Shift+Enter** inserts a newline (20 ms disambiguation window vs. Enter —
  terminals that send both as `\r` still work, they just can't do
  Shift+Enter).
- While a run is busy the prompt stays pinned above a live footer; your typed
  text never corrupts the streaming frame (the footer owns its screen rows
  with DECSTBM/DECSC so scrollback stays clean).

### Steering model

| Action | Meaning |
|---|---|
| Enter with text (mid-run) | **steer** — appended to the turn queue; consumed at the next model call |
| `/follow text` | **follow** — queued as a *new user turn* after the run ends |
| Ctrl+C / `/stop` | **abort** — the run stops; completed turns + partial stream stay in the session |
| approval prompt (sensitive bash) | answer inline; the run pauses at a safe boundary |

## 4. Provider & effort configuration

`.env` in the workspace (or the process environment; `.env.example` is the
documented template). Any number of providers, switched at runtime with
`provider <name>`.

### Per-provider block (`<PREFIX>`, e.g. `OPENAI`, `DEEPSEEK`, `ZHIPU`)

| Variable | Meaning |
|---|---|
| `<PREFIX>_API_KEY` | required (a block without a key is ignored) |
| `<PREFIX>_BASE_URL` | e.g. `https://api.openai.com/v1` (default) |
| `<PREFIX>_MODEL` | model name |
| `<PREFIX>_API` | `responses` (default, `POST /v1/responses`, `store=false`) or `chat` (`POST /v1/chat/completions`). Writing `.../v1/chat/completions` in the base URL also forces `chat`. |
| `<PREFIX>_REASONING_LEVELS` | explicit capability list, e.g. `off,minimal,low,medium,high,xhigh` (inferred from the model name if omitted) |
| `<PREFIX>_REASONING_EFFORT` | per-provider effort override |
| `<PREFIX>_CONTEXT_WINDOW` | token budget used by auto-compact (fallback: model-name table) |
| `<PREFIX>_INPUT_PRICE` / `_OUTPUT_PRICE` / `_CACHE_READ_PRICE` / `_CACHE_WRITE_PRICE` | USD per 1M tokens — feeds the cost meter and eval reports |

### Global variables

| Variable | Meaning |
|---|---|
| `DEFAULT_PROVIDER` | which block to start on |
| `MAX_TURNS` | REPL/one-shot turn cap; `0` = unlimited |
| `REASONING_EFFORT` | unified preference `off|minimal|low|medium|high|xhigh|max`; **clamped per model** (up first, then down) and omitted entirely for non-reasoning models (so they don't 400) |
| `WHEEL_TIMEOUT` | seconds per API call (default 180) |
| `WHEEL_API_RETRIES` / `WHEEL_API_RETRY_BASE` | transient-failure retry count / base backoff seconds |
| `WHEEL_AUTO_REFINE` | `8` = auto-refine cadence (same as `/refine auto 8`), `off` disables |
| `WHEEL_RUNS_DIR` | where `events.jsonl`/`responses.jsonl`/`meta.json` land (default `.wheel_runs` under the workspace) |
| `WHEEL_COLOR=0` / `NO_COLOR=1` | disable ANSI colors |
| `EXA_API_KEY` | optional: `web_search` uses the Exa REST API instead of the keyless (rate-limited) Exa MCP |
| `TAVILY_API_KEY` | optional: fallback search provider when Exa is unavailable or throttled |
| `PROVIDERS` / `MODEL` / `EFFORT` | bare fallbacks honored by the config loader: comma list of provider prefixes / model for the default provider / effort (after the prefixed and unified vars) |

Project context files `AGENTS.override.md` / `AGENTS.md` / `CLAUDE.md`
(searched from the workspace up to the git root, per-directory overrides first) enter the system prompt. Skills load from
`.wheel/skills`, `.agents/skills`, `skills/`, `~/.wheel/skills`,
`~/.agents/skills`; a project skill asks for trust once (decision stored in the workspace's `.wheel/trust.json`); `/skill:name` injects the full skill text.

## 5. Architecture & design rationale

This section is the "why each piece exists" map. Every module in
`wheel_agent/` has inline why-comments for the non-obvious parts; this is the
top-down view.

### 5.1 The loop (`loop.py`)

The whole agent is a while-loop:

```
system prompt (base + context files + harness + plan)
user turn
  → model call (streaming)
  → tool calls the model asked for (executed, results appended)
  → repeat until the model stops / cap hit / aborted
```

There is no planner, no sub-agent tree, no reflection phase. What makes it
*work* is bookkeeping done exactly right:

- **Turns** are the unit of accounting: each model call + its tool results is
  one turn; `MAX_TURNS`, `/max-turns`, and the eval caps all count turns.
- **Events** (`events.py`) are a flat stream — `say` (model text), `think`
  (reasoning), `tool_call`, `tool_result`, `usage`, `turn`, `run_start`,
  `run_end` — fanned out to the TTY renderer, the JSONL recorder, and the
  audit log by one `emit` call. The UI is a *view* over the event stream,
  not part of the loop.
- **Steering** is a queue (`queue.py`): the loop checks it between model
  calls; steer text becomes part of the next user message, follow text
  becomes a new user turn after the run. The loop never blocks on input —
  that's why Ctrl+C stays responsive mid-stream.
- **Plan mode**: the `plan` tool records steps; a step the model wants to
  execute is checked against the approved plan (mismatch → `plan_rejected`),
  which keeps multi-step work honest without an extra model call.

### 5.2 The session is a file (`session.py`)

A session is an **append-only JSONL tree** in the workspace (`.wheel/`):

```jsonl
{"type":"session","id":"s4f8...","created":1724000000}
{"type":"item","id":"i001","parent":null,"item":{"role":"user","content":"fix ..."}}
{"type":"item","id":"i002","parent":"i001","item":{"role":"assistant","content":[{"type":"function_call",...}]}}
{"type":"item","id":"i003","parent":"i002","item":{"role":"tool","content":[{"type":"function_call_output",...}]}}
```

- Every item is fsynced when written. **A crash costs at most the last torn
  line** — the loader skips a trailing line that isn't valid JSON. There is
  no WAL, no database, no lock file: the file *is* the log, and the tree
  shape (each item points at its parent) is what makes branches free.
- `/tree`, `/resume`, `/fork` are just tree walks. Jumping to a node and
  typing continues from there — a new branch. The `*` in the tree marks the
  current path.
- **Resume repair**: if a tool call's output never landed (crash between
  call and result), the loader injects a synthetic "interrupted" output, so
  the message sequence stays valid for the API.
- Compaction rewrites the file (overlay semantics) but keeps a `_saved`
  watermark so appends after a rewrite are never re-emitted.

### 5.3 Prompt-cache discipline (`compact.py`)

The API bills cache writes, and a cache invalidation mid-session costs both
money and latency. The rules:

1. **Never rewrite what was already sent.** Compaction cuts the history at a
   *user-message boundary*, replaces the prefix with one summary item, and
   keeps the recent items byte-identical. The suffix that the cache already
   holds stays valid.
2. **The cut point moves monotonically.** `cache_epoch` (exposed to the API
   as `prompt_cache_key`) bumps on every compaction; between compactions it
   is constant, so the provider's prefix cache stays hot.
3. **Auto-compact triggers on real usage**, not token estimates: when the
   provider-reported input tokens approach the context window (the footer
   meter shows the same number).
4. **The summary carries state tags** — `<read-files>` and
   `<modified-files>` blocks listing which files the prefix touched — so the
   model after compaction knows what it has already seen and changed. The
   summary itself is model-generated (a cheap pass), lossy by definition.
5. The identity no-op (compacting a tiny session) returns the history
   unchanged and bumps nothing.

### 5.4 Safety, approvals, undo (`safety.py`, `checkpoint.py`)

- **Sensitive paths** — `.env`, `.git`, `.ssh`, credentials, and friends —
  are refused for writes, and a **symlink pointing at one is refused too**
  (the check resolves the path, then re-checks every component).
- **Bash approvals are per intent**, not per string. The command is parsed
  into an intent (what verb, against which paths); you approve the intent
  once per session and the same intent doesn't re-prompt. `rm -rf`-class
  intents always prompt.
- **Undo without git**: every `write`/`edit` snapshots the previous file
  content first (`checkpoint.py`). `/undo [n]` restores the last n snapshots;
  `/undo-task` rolls back everything the last task changed, including files
  it created (delete) and files it rewrote. Binaries and files over 1 MB
  are skipped (sniffed, not guessed) to keep the store small.

### 5.5 Continual learning (`harness.py`, `refine.py`)

The model can store **durable notes/memories** via the `harness` tool —
into the global store (`~/.wheel/harness/`) or the session's store — and
they enter every future system prompt. This is the "the agent gets better
across sessions" loop:

- `/refine` runs a **cheap second model pass** over the transcript: "what
  durable lesson did this conversation establish?" The pass proposes
  structured edits to the harness.
- Every proposal carries a **CAS baseline** (the hash of the harness state it
  was based on). If the harness changed in between (you ran another refine,
  or the auto-refine thread fired), the stale proposal is **rejected, not
  applied** — no silent overwrite of a newer state.
- `/refine rollback <id>` reverts one refine's edits. Auto-refine runs in a
  background thread every N user turns (`/refine auto N`, default 8,
  `off` to disable); its output is queued as a normal event so it never
  interleaves with your typing.
- The harness state file is load-tolerant: corrupt JSON or a corrupt
  `schema` number degrades to empty state instead of crashing the app.

### 5.6 Tools (`tools.py`)

Deliberately small and boring — each tool is one file operation plus
validation:

| Tool | Behavior |
|---|---|
| `read` | read a file with offset/limit; long outputs spill to `.wheel/outputs/` with a pointer (the context never explodes) |
| `write` | create/overwrite (snapshot for undo first; sensitive-path check) |
| `edit` | exact `old_string` → `new_string`; **must match exactly once** (ambiguous = an error telling you to add context, or set `replace_all=true`) |
| `ls` / `glob` | listing / pattern match |
| `grep` | regex search, ripgrep-backed when `rg` is on PATH (faster, honors `.gitignore`) |
| `bash` | run a command (default 120 s foreground timeout); `background=true` returns a job_id instead; approval gate for sensitive intents |
| `bash_poll` | read a background job's output (`/jobs`, `/jobs kill` manage them) |
| `skill` | load a skill's full text into context |
| `harness` | read/write the notes/memories store |
| `plan` | create/update/complete plan steps (drives `plan_rejected`) |
| `web_fetch` / `web_search` | fetch a URL to text (SSRF-guarded, no-redirect opener) / web search via Exa (REST API with `EXA_API_KEY`, else keyless Exa MCP) with Tavily fallback (`TAVILY_API_KEY`) when Exa is throttled |

Long tool outputs are truncated with a stable prefix/suffix and the full
text written to disk (`truncate.py`); `/expand <run>` reprints it.

### 5.7 Live UI (`style.py`, `repl.py`, `app/live.py`)

- Model text streams as it arrives (say frames; think frames dimmer).
- Tool calls render as compact lines; outputs are clipped to a few lines
  with an `/expand` pointer.
- The **footer meter** (context usage, cost, turn, model) owns its screen
  rows via DECSTBM (scroll-region) + DECSC/DECRC (cursor save) so streaming
  never corrupts it and resize (SIGWINCH) re-lays it out without tearing.
  Size comes from `ioctl`, never from a stale `COLUMNS` env.
- The same event printer serves `/replay` — a replay *is* the same UI fed
  from the recorded event stream.

### 5.8 Replay (`replay.py`)

Every run records `events.jsonl` (what happened) and `responses.jsonl`
(raw model responses). Replaying means: fresh workspace, same task, but the
model is replaced by the **recorded responses**. The run is then classified:

| Class | Meaning |
|---|---|
| `exact` | identical tool decisions, stop reason, and workspace fingerprint |
| `behavioral` | same behavior modulo timing/nondeterminism |
| `drift` | diverged but completed |
| `error` | replay crashed |

This is the determinism audit behind every eval number: a 100% resolve rate
with 0% `exact` replay would mean the benchmark is measuring the model's
variety, not the harness.

### 5.9 Module map

```
wheel_agent/
  core/           the agent core
    loop.py         the agent loop
    model.py        Responses + Chat Completions clients (stream, retry, cancel)
    reasoning.py    effort scale, clamp, per-API payloads
    prompt.py       system prompt assembly
    context.py      token estimation, context files, skill expansion
    session.py      JSONL tree store
    compact.py      prompt-cache-aware compaction
    checkpoint.py   file snapshots → /undo /undo-task
    truncate.py     output clipping + spill-to-disk
    plan.py         plan steps + rejection
    events.py       event stream + JSONL recorder
    queue.py        steer/follow/abort queue
    meter.py        turns/tokens/cost meter
    config.py       .env → AgentConfig
    types.py        shared dataclasses
  tools/          the tool layer
    tools.py        the 13 tools + background jobs
    safety.py       sensitive paths + bash intent approval
    trust.py        project-skill trust
    audit.py        run audit (hashes, fingerprints, permission verdicts)
    workspace.py    workspace fingerprinting
    rgfiles.py      glob/grep (ripgrep when available)
    atfiles.py      @file mention expansion
    web.py          SSRF-guarded fetch/search
  ui/             terminal UI
    repl.py         the REPL (input editor, busy prompt, dispatch)
    style.py        TTY styling, ansi, term size
    markdown.py     terminal markdown rendering
    graph.py        session DAG → ascii/HTML
    replay.py       recorded-response replay
    app/            the TUI process
      state.py        AppState (footer/live/active/snips/refine thread state)
      live.py         LiveTurn, ToolSnips, print_event, clip/expand, meter
      commands.py     /resume /tree /graph /compact /harness /jobs /undo /replay
      refine.py       manual + auto refine (one execution core, two presentations)
      __init__.py     process plumbing: config load, run_task, --json, session CLI, dispatch
  harness/        continual learning
    harness.py      notes/memories store (CAS, rollback, load-tolerant)
    refine.py       lesson extraction (second model pass)
```

## 6. Testing

```bash
scripts/run_all_tests.sh     # CI entry point: 272 pytest + keyless PTY scenarios
python3 -m pytest            # unit/integration only (no key, no network)
pip install -e ".[pty]"
python scripts/pty_eval.py --only boot,slash,steer_stop,skill_trust,eof   # keyless PTY
python scripts/pty_eval.py                       # all 15 scenarios (spends tokens)
```

The PTY suite drives the *real* REPL through a pty (not `--json`) and
separates `pass` / `model_fail` (no provider configured — expected keyless)
/ `harness_fail` (a bug in the UI itself; the only one that fails CI). The
full feature × boundary matrix, the scenario list, and the manual checklist
are in [TESTING.md](TESTING.md) and [MANUAL-TEST.md](MANUAL-TEST.md).

CI (`.github/workflows/ci.yml`): pytest + keyless PTY on Linux/macOS across
Python 3.11–3.13, pytest-only on Windows (pexpect needs a POSIX pty). No API
key, no Docker.

## 7. Repository layout

```
wheel_agent/       the agent (see 5.9)
  core/            loop, model, session, context, compaction, config, types
  tools/           the 13 tools + safety/trust/audit, workspace, file & web access
  ui/              terminal UI: repl, style, markdown, graph, replay
  ui/app/          the TUI process: state, live, commands, refine
  harness/         notes/memories store + lesson extraction
README.md          this file
```

## 8. Limitations (honest list)

- One provider at a time per run; switching is per-process (`provider <name>`).
- No MCP, no sub-agent fan-out, no image input — by design, to keep the loop readable.
- The audit re-hashes the workspace each turn; on very large workspaces that bookkeeping is visible.
- SWE-bench Lite needs the official Docker images (tens of GB); the default set is 5 curated instances, not all 300.
- Compaction summaries are lossy by definition; quality is bounded by the summarizer model.
- The `wheel` console-script name collides with pip's `wheel` utility (see 1).
- Chinese-first in the TUI strings; the code and docs are bilingual.

## License

[MIT](LICENSE)

---

# 中文附录（详细版）

**Wheel Agent**：极简但完整的 Python Coding Agent。一个本地 agent loop +
任意 OpenAI 兼容端点（Responses / Chat Completions）+ 可崩溃恢复的分叉会话
文件 + 对 prompt cache 友好的压缩 + 文件级 undo + 持续学习 harness + 可回放
评测（Aider Polyglot 免 Docker；SWE-bench Lite 走官方 harness）。

目的不是刷榜，而是让 agent harness 的**每个部分都可见、可读**：约 12k 行
Python，运行时依赖只有 `openai` SDK + `python-dotenv`，无框架、无 MCP、无
RPC——一个循环、一个文件、一个终端。想真正看懂 coding agent 从循环、流式、
缓存、压缩、安全、undo、回放到评测的完整链路，这就是该读的代码库。

## 安装与运行

见上文 Quickstart。要点：`pip install -e ".[dev]"` 后 `cp .env.example .env`
填 key；`wheel` 起 REPL（工作区=当前目录，没有 `--workspace` 参数，先 cd）；
`wheel "任务"` 一次性执行；`wheel --json "任务"` 输出一行 JSON（字段：
`text/stop_reason/run_id/task_id/session_id/usage/changed_files`，退出码
0=正常停止、1=异常停止、2=配置错误）。

**注意**：命令叫 `wheel`，和 pip 的 `wheel` 工具重名；装进 venv 后 PATH 里
它优先。需要 pip 的 wheel 时用 `python3 -m wheel`。

交互中：**回车=steer**（并入下一次模型调用）；`/follow 文本`=停机后作为新
用户回合投递；`Ctrl+C`/`/stop`=中止（已完成的 turn 保留）。完整斜杠命令表见上文（含全部别名，`/tree <id>` 跳转即分叉、`/refine auto N` 后台抽取、`/undo-task` 事务回滚、`/graph html` 出 DAG 网页等）。

## 核心设计（为什么这么写）

- **循环**（`loop.py`）：while 循环——问模型、执行它要的工具、把结果追加
  回去、直到它停。没有 planner/子 agent/反思阶段，本事全在模型，其余是
  记账：turn 是计量单位；事件流（say/think/tool_call/tool_result/usage）
  一个 `emit` 同时喂 TTY、JSONL 记录器和审计；steer/follow 是队列，循环
  在两次模型调用之间检查，所以 Ctrl+C 永远保持响应。
- **会话即文件**（`session.py`）：append-only JSONL 树，每条 fsync；崩溃最
  多丢最后一行 torn line（读取端跳过）。树形结构让分叉/恢复/undo 免费
  （`/tree` 只是渲染器）。工具调用有输出缺失时补合成 "interrupted"，保证
  resume 后消息序列对 API 合法。压缩重写文件但带 `_saved` 水位，重写后的
  追加不会重复落盘。
- **Prompt cache 纪律**（`compact.py`）：压缩只在**用户消息边界**切、前缀
  换成一条摘要、后缀逐字节不动（已发的不重写）；`cache_epoch`
  （即 API 的 `prompt_cache_key`）只在压缩时递增，其余时间前缀缓存保持
  热；自动压缩用 provider 返回的**真实 usage** 触发，不靠估算；摘要带
  `<read-files>`/`<modified-files>` 标签，压缩后的模型知道之前碰过哪些文件。
- **安全与 undo**：`.env`/`.git` 等敏感路径拒写（含指向它们的符号链接，
  逐组件检查）；bash 审批按**意图**（对哪些路径做什么）记一次会话内不重
  问；`write`/`edit` 前快照 → `/undo [n]`、`/undo-task` 不依赖 git；二进制
  和 >1MB 文件按嗅探跳过。
- **持续学习**（`harness.py`/`refine.py`）：`harness` 工具写 notes/memories
  （全局 `~/.wheel/harness/` 或会话级），进入后续系统提示。`/refine` 用
  便宜的第二遍模型从轨迹抽教训，提案带 **CAS 基线**（harness 变了就拒绝
  而非覆盖）；`/refine rollback <id>` 回滚；`/refine auto N` 后台每 N 个用
  户回合抽取一次（默认 8）。harness 状态文件损坏时降级为空状态而非崩 app。
- **工具**（`tools.py`）：13 个，刻意小而无聊：read（长输出溢出到
  `.wheel/outputs/` 留指针）、write、edit（`old_string` 必须**恰好匹配一
  次**，歧义是错误不是赌）、ls/glob/grep（有 ripgrep 就用）、bash +
  bash_poll（后台作业 + `/jobs`）、skill、harness、plan（步骤不匹配批准计
  划 → `plan_rejected`）、web_fetch/web_search（SSRF 防护 + no-redirect
  opener，Exa 可选）。
- **回放**（`replay.py`）：每次 run 记 `events.jsonl`+`responses.jsonl`；
  回放=新工作区+同任务+**用录好的模型响应替换模型**，然后分类
  `exact`（工具决策+停止原因+工作区指纹全同）/`behavioral`/`drift`/
  `error`。这是所有评测数字背后的确定性审计。
- **实时 UI**：流式 say/think 帧、工具输出裁剪+`/expand`、底部 meter 用
  DECSTBM/DECSC 独占屏幕行（流式与 resize 都不撕裂，尺寸来自 ioctl 而不
  是 stale `COLUMNS`）；`/replay` 就是同一套事件打印机喂录制流。

## 测试

`scripts/run_all_tests.sh` 是统一入口（CI 同款，无 key 无 Docker）：
272 pytest + 免 key PTY 场景（`boot,slash,steer_stop,skill_trust,eof`）。
PTY 套件驱动**真实 REPL**（不是 `--json`），把 `pass` / `model_fail`（没
配模型，免 key 时预期如此）/ `harness_fail`（界面本身的 bug，唯一会让 CI
挂的）分开报告。功能×边界完整矩阵见 [TESTING.md](TESTING.md)，人工点选
清单见 [MANUAL-TEST.md](MANUAL-TEST.md)。CI：`.github/workflows/ci.yml`，
Linux/macOS × Python 3.11–3.13 跑全量，Windows 只跑 pytest（pexpect 需要
POSIX pty）。

## 局限

一次一个 provider；无 MCP/子 agent/图像输入（刻意保持循环可读）；审计每轮
对大工作区全量哈希，超大仓库会慢；SWE Lite 默认只跑精选 5 题（官方镜像几
十 GB）；压缩摘要由定义即有损；`wheel` 命令与 pip 工具重名；TUI 文案中
文优先。
