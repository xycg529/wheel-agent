from __future__ import annotations

import json
import os
import select
import signal
import sys
import threading
from pathlib import Path

from wheel_agent import style
from wheel_agent.checkpoint import CheckpointStore
from wheel_agent.compact import SUMMARY_MARK, compact_history, is_summary_item
from wheel_agent.config import AgentConfig, load_config, provider_ready
from wheel_agent.context import expand_skill_command, load_project_files, load_skills
from wheel_agent.trust import ensure_project_trust
from wheel_agent.atfiles import list_at_files
from wheel_agent.events import list_run_ids, load_run
from wheel_agent.harness import HarnessStore, format_harness_for_prompt
from wheel_agent.loop import run_agent
from wheel_agent.markdown import render_markdown
from wheel_agent.meter import compact_count, format_meter
from wheel_agent.model import make_client, extract_text, extract_thinking
from wheel_agent.queue import TurnQueue
from wheel_agent.reasoning import clamp_effort, normalize, reasoning_payload
from wheel_agent.repl import BusyPrompt, LineEditor, _read_key, completion_words, enter_busy_tty, is_busy_abort_key, pick_list, query_cursor_row
from wheel_agent.graph import build_session_graph, render_ascii, serve_graphs, stop_graph_server, write_html
from wheel_agent.replay import print_timeline, replay_run, replay_session
from wheel_agent.refine import (
    format_refine_result,
    parse_auto_refine_every,
    parse_refine_args,
    refine_due,
    run_refine,
)
from wheel_agent.tools import drain_job_events, format_jobs, kill_job
from wheel_agent.session import Session, _item_plain_text

FOOTER = style.Footer()
LIVE = None  # set per turn
ACTIVE: dict = {"thread": None, "queue": None, "runtime": None, "model": None, "session": None}
AUTO_REFINE_EVERY = 8
_refine_at: dict[str, int] = {}
_refine_lock = threading.Lock()
_refine_pending: list[dict] = []
_refine_thread: threading.Thread | None = None
UI_TOOL_LINES = 6
UI_TOOL_CHARS = 500


class ToolSnips:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self._n = 0

    def add(self, name: str, output: str, turn: object = None) -> dict:
        self._n += 1
        rec = {"n": self._n, "id": f"r{self._n}", "name": name, "output": output, "turn": turn}
        self.items.append(rec)
        if len(self.items) > 40:
            self.items = self.items[-40:]
        return rec

    def get(self, spec: str | int | None = None) -> dict | None:
        if not self.items:
            return None
        if spec is None or spec == "":
            return self.items[-1]
        key = str(spec).strip().lower()
        if key in {"last", "l"}:
            return self.items[-1]
        rid = key[1:] if key.startswith("r") and key[1:].isdigit() else key
        if rid.isdigit():
            n = int(rid)
            for rec in reversed(self.items):
                if rec["n"] == n:
                    return rec
        return None


SNIPS = ToolSnips()


HELP = """\
Wheel — 轮子，Loop 绕着转。普通输入当任务；斜杠是指令
  /help                 本说明
  /quit  /exit  /q      退出（Ctrl+C / Ctrl+D 同样退出）
  /provider             ↑↓ 选择并切换 Provider
  /provider <name>      直接切换 Provider
  /effort  /think       ↑↓ 选择当前模型支持的推理强度
  /effort <level>       仅列出 .env 里 *_REASONING_LEVELS 的档位
  /compact              立刻压缩当前会话历史
  /undo [n]             撤销最近一次（或 n 次）write/edit
  /undo-task            一次回滚最近任务的全部文件变更
  /replay               ↑↓ 选择一次 run 的时间线
  /replay <run_id>      打印录制时间线
  /replay <run_id> go   用录制输出重跑这一次 task
  /replay session       按本 session 全部 run 顺序重放（写到 .wheel/session-replay/）
  /new                  新开会话
  /sessions             列出本项目会话（id + 首条用户消息）
  /resume               列出会话，上下键选择后回车恢复
  /resume [id]          恢复指定会话并重放对话
  /plan                 打印当前 plan
  /harness              打印 continual harness（prompt notes / memories）
  /refine [text]        从当前轨迹提取可复用教训
  /refine --global      写入跨会话全局 harness
  /refine rollback <id> 回滚一次 refine
  /refine auto [N|off]  每 N 个用户回合后台抽取（默认 8，off=关）
  /jobs                 列出后台 bash
  /jobs kill            ↑↓ 选择并杀掉一个后台作业
  /tree                 会话树，↑↓ 回车跳转到某条用户消息
  /tree <id>            直接跳转到该条用户消息
  /graph                当前路径的 turn/工具 DAG（文本）
  /graph html           写出 HTML，只打印路径和本地 http 地址
  /fork [id]            同 /tree
  /follow <text>        本轮停机后再投递
  /stop                 中止当前任务（Ctrl+C 同样）
  /expand [r12]         展开最近一条，或指定 r编号
  /skill:name           注入该 skill 全文并当作任务
  /max-turns [n]        查看或设置 turn 上限（0=不限）

运行中底部仍有 `>`：回车 = steer（下一轮模型调用就吃到），/follow 等本轮正常停再投。
/stop、Ctrl+C、Esc = abort。输入只出现在 `>` 里，不会改写 say/think。
多行粘贴会收成一条任务，不会把后几行当成 steer。
"""


class LiveTurn:
    def __init__(self) -> None:
        self.text = False
        self.thinking = False
        self.bash = False
        self._open: str | None = None
        self._text = ""
        self._think = ""
        self._waiting = False

    def reset(self) -> None:
        self.close()
        self.text = False
        self.thinking = False
        self.bash = False
        self._text = ""
        self._think = ""

    def show_wait(self) -> None:
        if self._waiting or self._open or not style.is_tty():
            return
        # Commit a full line so it stays in the scroll region, not the `>` row.
        style.writeln(style.dim("Working..."))
        self._waiting = True

    def clear_wait(self) -> None:
        if not self._waiting:
            return
        self._waiting = False
        if style.is_tty():
            # writeln left the cursor on the next line; erase that empty row
            # and the Working... row.
            style.replace_last_rows(2, "", reserved_bottom=FOOTER.height())

    def close(self) -> None:
        self.clear_wait()
        if self._open == "text":
            self.finish_say(self._text)
            return
        if self._open == "thinking":
            self.finish_think(self._think)
            return
        self._open = None

    def abandon_say(self) -> None:
        """Close a live say/think block without reprinting it as a finished reply."""
        self.clear_wait()
        if self._open in {"text", "thinking", "bash"}:
            style.writeln()
            style.writeln(style.dim("└"))
        self._open = None
        self._text = ""
        self.text = True

    def finish_say(self, text: str) -> None:
        body = text or self._text
        rendered = style.frame("say", render_markdown(body))
        if self._open == "text" and style.is_tty():
            style.replace_last_rows(
                style.open_block_rows(self._text), rendered, reserved_bottom=FOOTER.height()
            )
        else:
            _emit(rendered)
        self._open = None
        self.text = True

    def finish_think(self, text: str) -> None:
        body = (text or self._think).strip()
        if body in {"", "…", "..."}:
            if self._open == "thinking" and style.is_tty() and self._think:
                style.replace_last_rows(
                    style.open_block_rows(self._think), "", reserved_bottom=FOOTER.height()
                )
            self._open = None
            self.thinking = True
            return
        rendered = style.frame("think", render_markdown(body), paint=style.magenta)
        if self._open == "thinking" and style.is_tty():
            style.replace_last_rows(
                style.open_block_rows(self._think), rendered, reserved_bottom=FOOTER.height()
            )
        else:
            _emit(rendered)
        self._open = None
        self.thinking = True

    def on_delta(self, kind: str, chunk: str) -> None:
        if kind == "thinking":
            if not chunk:
                return
            self.clear_wait()
            if self._open == "text":
                self.finish_say(self._text)
            if self._open != "thinking":
                style.writeln(style.dim("┌ ") + style.magenta(style.bold("think")))
                self._open = "thinking"
            self._think += chunk
            style.stream_write(style.crlf(chunk) if "\n" in chunk else chunk)
            self.thinking = True
        elif kind == "text":
            self.clear_wait()
            if self._open == "thinking":
                self.finish_think(self._think)
            self._text += chunk
            if not style.is_tty():
                return
            if self._open != "text":
                style.writeln(style.dim("┌ ") + style.bold("say"))
                self._open = "text"
            style.stream_write(style.crlf(chunk) if "\n" in chunk else chunk)
            self.text = True

    def on_tool_update(self, chunk: str) -> None:
        del chunk
        self.clear_wait()
        if self._open == "text":
            self.finish_say(self._text)
        elif self._open == "thinking":
            self.finish_think(self._think)
        self.bash = True
        self._open = "bash"


def _effort_line(config: AgentConfig) -> str:
    payload = reasoning_payload(config.effort, config.provider.effort_levels)
    level = payload["effort"] if payload else "off"
    return f"effort  {level}"


def _effort_choices(config: AgentConfig) -> tuple[str, ...]:
    return tuple(config.provider.effort_levels)


def _completion_words(config: AgentConfig, workspace, trusted: bool) -> list[str]:
    return completion_words(
        config.providers,
        (s.name for s in load_skills(workspace, trusted=trusted)),
        effort_levels=_effort_choices(config),
    )


def ask_yes_no(prompt: str) -> bool:
    queue = ACTIVE.get("queue")
    if queue is not None and threading.current_thread() is not threading.main_thread():
        return queue.request_ask(prompt)
    return _ask_on_main(prompt)


def _ask_on_main(prompt: str) -> bool:
    """Yes/no without input(): nested readline hides the next > prompt."""
    _emit(style.yellow(prompt))
    sys.stdout.write(style.dim("proceed? [y/N] "))
    sys.stdout.flush()
    try:
        line = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write("\n")
        sys.stdout.flush()
        FOOTER.paint()
        return False
    # Finish the prompt line so the next ┌ ok / error block cannot glue onto it.
    sys.stdout.write("\n")
    sys.stdout.flush()
    FOOTER.paint()
    if line == "":
        return False
    return line.strip().lower() in {"y", "yes"}


def _meter_text(config: AgentConfig, session: Session, last=None) -> str:
    provider = config.provider
    return format_meter(
        session.usage,
        last or session.usage,
        context_window=provider.context_window,
        input_price=provider.input_price,
        output_price=provider.output_price,
        cache_read_price=provider.cache_read_price,
        cache_write_price=provider.cache_write_price,
        compact_runs=session.compactions,
    )


def _emit(*args, **kwargs) -> None:
    del kwargs
    # Do not FOOTER.paint() here: CUP to the last rows between prints is what
    # glued resume blocks together when the rule wrapped.
    style.writeln_wrapped(" ".join(str(a) for a in args) if args else "")


def parse_tool_args(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"arguments": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"arguments": raw}
    return {}


def _format_args(args: object, name: str | None = None) -> str:
    parsed = dict(args) if isinstance(args, dict) else dict(parse_tool_args(args))
    if name == "ls" and not str(parsed.get("path") or "").strip():
        parsed["path"] = "."
    if not parsed:
        return ""
    lines = []
    for key, value in parsed.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if key in {"content", "new_string", "old_string"} and len(text) > 80:
            text = f"({len(text)} chars) {text[:60]}..."
        elif len(text) > 120:
            text = text[:117] + "..."
        lines.append(f"{key}: {text}")
    return "\n".join(lines)


def tool_output_label(item: dict) -> tuple[str, object]:
    output = str(item.get("output") or "")
    blocked = bool(item.get("blocked")) or output.startswith("blocked by safety")
    error = bool(item.get("is_error")) or output.startswith(
        ("unknown tool:", "PermissionError:", "invalid arguments:")
    )
    if blocked:
        return "blocked", style.yellow
    if error:
        return "error", style.red
    return "ok", style.green


def clip_tool_output(text: str, max_lines: int = UI_TOOL_LINES, max_chars: int = UI_TOOL_CHARS) -> tuple[str, int]:
    original = text.splitlines() or [""]
    total = len(original)
    shown = original
    if len(text) > max_chars:
        cut = text[:max_chars]
        shown = cut.splitlines() or [""]
    if len(shown) > max_lines:
        shown = shown[:max_lines]
    omitted = max(0, total - len(shown))
    body = "\n".join(shown) if shown else ""
    if omitted:
        body = (body + "\n" if body else "") + f"… +{omitted} lines"
    return body, omitted


def _emit_clip(name: str, label: str, output: str, paint) -> None:
    rec = SNIPS.add(name, output)
    body, omitted = clip_tool_output(output)
    if omitted:
        body = f"{body}\n/expand {rec['id']}"
    _emit(style.prefix_block(f"{label}  {rec['id']}  {name}", body or " ", paint))
    FOOTER.paint()


def print_event(event: dict) -> None:
    global LIVE
    kind = event.get("type")
    queue = ACTIVE.get("queue")
    if queue is not None and queue.abort.is_set() and kind not in {"error", "agent_end"}:
        return
    live = LIVE or LiveTurn()
    if kind == "turn_start":
        live.reset()
        LIVE = live
        if event.get("user", True):
            label = event.get("display_turn") or event.get("turn")
            _emit()
            _emit(style.bold(style.cyan(f"── turn {label} ──")))
            sys.stdout.flush()
    elif kind == "message_start":
        LIVE = live
        live.show_wait()
    elif kind == "message_end":
        thinking = (event.get("thinking") or "").strip()
        text = (event.get("text") or "").strip()
        if event.get("hide_text"):
            live.abandon_say()
        elif live._open == "text":
            live.finish_say(text)
        elif live._open == "thinking":
            live.finish_think(thinking or live._think)
        else:
            live.close()
        if thinking and not live.thinking and not event.get("hide_text"):
            _emit(style.frame("think", render_markdown(thinking), paint=style.magenta))
        if text and not live.text and not event.get("hide_text"):
            _emit(style.frame("say", render_markdown(text)))
        FOOTER.paint()
    elif kind == "plan_rejected":
        live.abandon_say()
        _emit()
        _emit(style.yellow("plan rejected — stopped. Next message revises the plan."))
        _sync_plan_footer(busy=False)
        FOOTER.paint()
    elif kind == "tool_execution_start":
        live.close()
        live.bash = False
        name = str(event.get("tool_name") or "tool")
        body = _format_args(event.get("args"), name)
        _emit(style.prefix_block(f"tool  {name}", body or " ", style.yellow))
    elif kind == "tool_execution_end":
        if event.get("blocked"):
            label, paint = "blocked", style.yellow
        elif event.get("is_error"):
            label, paint = "error", style.red
        else:
            label, paint = "ok", style.green
        output = str(event.get("result") or "")
        name = str(event.get("tool_name") or "tool")
        rec = SNIPS.add(name, output, event.get("turn"))
        body, omitted = clip_tool_output(output)
        if omitted:
            body = f"{body}\n/expand {rec['id']}"
        live.bash = False
        live._open = None
        _emit(style.prefix_block(f"{label}  {rec['id']}  {name}", body or " ", paint))
        if name == "plan":
            _sync_plan_footer()
        FOOTER.paint()
    elif kind == "error":
        live.close()
        msg = str(event.get("message") or "error")
        _emit(style.red(msg))
        if event.get("transient"):
            _emit(style.dim("provider hiccup — session kept, send the task again or try another provider"))
    elif kind == "api_retry":
        live.close()
        _emit(style.dim(f"retry {event.get('attempt')} — {event.get('message')}"))
        live.show_wait()
    elif kind == "compact" and event.get("did"):
        live.close()
        before_t = compact_count(int(event.get("before_tokens") or 0))
        after_t = compact_count(int(event.get("after_tokens") or 0))
        _emit(
            style.dim(
                f"compact  {event.get('before_items')} → {event.get('after_items')} items  "
                f"~{before_t} → ~{after_t} tok  epoch {event.get('epoch')}"
            )
        )
        FOOTER.paint()


def _busy() -> bool:
    thread = ACTIVE.get("thread")
    return bool(thread and thread.is_alive())


def _sync_plan_footer(*, busy: bool | None = None, session: Session | None = None) -> None:
    chat = session or ACTIVE.get("session")
    plan = getattr(chat, "plan", None)
    if plan is None:
        runtime = ACTIVE.get("runtime")
        plan = getattr(runtime, "plan", None)
    showing = _busy() if busy is None else busy
    lines = plan.footer_lines(busy=showing) if plan is not None else []
    FOOTER.set_plan(lines)


def run_task(
    config: AgentConfig,
    task: str,
    workspace: Path,
    session: Session,
    queue: TurnQueue | None = None,
) -> None:
    global LIVE
    if not provider_ready(config.provider):
        print(
            style.red(
                f"provider {config.provider.name} has no API key. "
                f"Set {config.provider.name.upper()}_API_KEY in .env"
            )
        )
        return
    LIVE = LiveTurn()
    ACTIVE["session"] = session
    session.plan.ask = ask_yes_no
    session.plan.interactive = True
    _sync_plan_footer(busy=True, session=session)
    model = make_client(config.provider, effort=config.effort, cache_key=session.cache_key)
    if queue is not None:
        model.abort = queue.abort
    ACTIVE["model"] = model
    model.on_retry = lambda attempt, message: print_event(
        {"type": "api_retry", "attempt": attempt, "message": message}
    )

    def on_delta(kind: str, chunk: str) -> None:
        if queue is not None and queue.abort.is_set():
            return
        LIVE.on_delta(kind, chunk)

    def on_tool_update(chunk: str) -> None:
        if queue is not None and queue.abort.is_set():
            return
        LIVE.on_tool_update(chunk)

    result = run_agent(
        task,
        workspace,
        config,
        model,
        ask=ask_yes_no,
        on_event=print_event,
        on_delta=on_delta,
        on_tool_update=on_tool_update,
        turn_offset=session.turn_offset,
        extra_meta={"session_id": session.session_id},
        queue=queue,
        session=session,
        plan=session.plan,
        runtime_out=ACTIVE,
    )
    LIVE.close()
    ACTIVE["last_task_id"] = result.task_id
    session.turn_offset += result.turns
    session.usage.add(result.usage)
    session.persist(rewrite=True)
    _sync_plan_footer(busy=False, session=session)
    FOOTER.set(_meter_text(config, session, result.last_usage))
    if config.interactive:
        maybe_schedule_periodic_refine(config, workspace, session)


def run_json_task(config: AgentConfig, task: str, workspace: Path) -> int:
    chat = Session.create(workspace)
    if not provider_ready(config.provider):
        sys.stdout.write(
            json.dumps(
                {"error": f"missing API key for {config.provider.name}", "stop_reason": "error"},
                ensure_ascii=False,
            )
            + "\n"
        )
        return 2
    model = make_client(config.provider, effort=config.effort, cache_key=chat.cache_key)
    result = run_agent(
        task,
        workspace,
        config,
        model,
        extra_meta={"session_id": chat.session_id, "json": True},
        session=chat,
        plan=chat.plan,
    )
    chat.turn_offset += result.turns
    chat.usage.add(result.usage)
    chat.persist(rewrite=True)
    payload = {
        "text": result.text,
        "stop_reason": result.stop_reason,
        "run_id": result.run_id,
        "task_id": result.task_id,
        "session_id": chat.session_id,
        "usage": result.usage.as_dict(),
        "changed_files": result.changed_files,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return 0 if result.stop_reason in {"stop", "max_turns", "plan_rejected"} else 1


def handle_replay_session(config: AgentConfig, session: Session, workspace: Path, dest_spec: str = "") -> None:
    spec = dest_spec.strip()
    if spec.lower() in {"", "go"}:
        dest = workspace / ".wheel" / "session-replay" / session.session_id
    else:
        dest = Path(spec).expanduser()
        if not dest.is_absolute():
            dest = (workspace / dest).resolve()
    print(style.dim(f"session replay {session.session_id} → {dest}"))
    try:
        results = replay_session(
            config.runs_dir,
            session.session_id,
            dest,
            source_workspace=workspace,
        )
    except FileNotFoundError as exc:
        print(style.red(str(exc)))
        return
    for result in results:
        status = result.replay_status or "unknown"
        print(f"[{status}] {result.run_id} stop={result.stop_reason}")
        if result.replay_details:
            print(style.dim(json.dumps(result.replay_details, ensure_ascii=False, sort_keys=True)))


def handle_replay(config: AgentConfig, run_id: str, workspace: Path, execute: bool) -> None:
    try:
        bus = load_run(config.runs_dir, run_id)
    except FileNotFoundError as exc:
        print(style.red(str(exc)))
        print(style.dim("hint: /replay wants a .wheel_runs id, not a session id; session ids work if a run recorded session_id"))
        return
    if bus.run_id != run_id:
        print(style.dim(f"resolved {run_id} → run {bus.run_id}"))
    print(print_timeline(bus), end="")
    if execute:
        timeline, result = replay_run(config.runs_dir, bus.run_id, workspace, interactive=False)
        del timeline
        print(f"replayed as {result.run_id} stop={result.stop_reason} status={result.replay_status or 'unknown'}")
        if result.replay_details:
            print(style.dim(json.dumps(result.replay_details, ensure_ascii=False, sort_keys=True)))


def print_transcript(session: Session) -> None:
    items = session.view_items()
    if not items:
        print(style.dim("(empty session)"))
        return
    SNIPS.items.clear()
    SNIPS._n = 0
    calls: dict[str, str] = {}
    user_n = 0
    for item in items:
        kind = item.get("type")
        role = item.get("role")
        if kind in {"reasoning", "thinking"}:
            body = extract_thinking([item])
            if body:
                _emit(style.frame("think", render_markdown(body), paint=style.magenta))
            continue
        if role == "user":
            text = _item_plain_text(item)
            if is_summary_item(item):
                _emit(style.prefix_block("summary", text.replace(SUMMARY_MARK, "").strip() or "compacted", style.dim))
            else:
                user_n += 1
                _emit()
                _emit(style.bold(style.cyan(f"── turn {user_n} ──")))
                _emit(style.prefix_block("you", text, style.cyan))
            continue
        if kind == "function_call":
            name = str(item.get("name") or "tool")
            cid = str(item.get("call_id") or "")
            if cid:
                calls[cid] = name
            body = _format_args(parse_tool_args(item.get("arguments") or item.get("args")), name)
            _emit(style.prefix_block(f"tool  {name}", body or " ", style.yellow))
            continue
        if kind == "function_call_output":
            cid = str(item.get("call_id") or "")
            name = calls.pop(cid, "tool")
            output = str(item.get("output") or "")
            rec = SNIPS.add(name, output)
            body, omitted = clip_tool_output(output)
            if omitted:
                body = f"{body}\n/expand {rec['id']}"
            label, paint = tool_output_label(item)
            _emit(style.prefix_block(f"{label}  {rec['id']}  {name}", body or " ", paint))
            continue
        think = extract_thinking([item])
        text = extract_text([item])
        if not text and role == "assistant":
            text = _item_plain_text(item)
        if think:
            _emit(style.frame("think", render_markdown(think), paint=style.magenta))
        if text:
            _emit(style.frame("say", render_markdown(text)))
    FOOTER.paint()


def handle_resume(workspace: Path, rest: str, current: Session) -> Session:
    spec = rest.strip()
    try:
        if spec:
            chat = Session.load_id(workspace, spec)
        else:
            rows = Session.list_previews(workspace)
            if not rows:
                print(style.dim("(no sessions)"))
                return current
            labels = [f"{sid}  {preview}" for sid, preview in rows]
            selected = next((i for i, (sid, _) in enumerate(rows) if sid == current.session_id), 0)
            picked = pick_list(labels, selected)
            if picked is None:
                return current
            chat = Session.load_id(workspace, rows[picked][0])
    except FileNotFoundError as exc:
        print(style.red(str(exc)))
        return current
    style.writeln_wrapped(style.green(f"resumed {chat.session_id}  ({chat.user_turns()} user turns)"))
    print_transcript(chat)
    return chat


def _tree_option(row: dict) -> str:
    mark = "*" if row["on_path"] else " "
    indent = "  " * int(row["depth"])
    return f"{mark} {indent}{row['id']}  {row['label']}"


def handle_tree(session: Session, target: str | None = "", *, jumping: bool = False) -> bool:
    spec = (target or "").strip()
    rows = session.tree_rows()
    if not spec and not jumping:
        if not rows:
            print(style.dim("(empty)"))
            return True
        labels = [_tree_option(row) for row in rows]
        selected = next((i for i, row in enumerate(rows) if row["leaf"]), 0)
        picked = pick_list(labels, selected)
        if picked is None:
            return True
        spec = rows[picked]["id"]
    if spec or jumping:
        try:
            session.fork(spec or None)
        except (KeyError, ValueError) as exc:
            print(style.red(str(exc)))
            return True
        session.persist(rewrite=True)
        print(style.green(f"now at {session.leaf_id}  ({session.user_turns()} user turns)"))
    rows = session.tree_rows()
    if not rows:
        print(style.dim("(empty)"))
        return True
    for row in rows:
        print(_tree_option(row))
    return True


def handle_graph(session: Session, workspace: Path, runs_dir: Path, rest: str = "") -> None:
    graph = build_session_graph(session, runs_dir)
    if not graph.layers and graph.tree.empty():
        print(style.dim("(empty session)"))
        return
    if rest.strip().lower() in {"html", "open", "web", "serve"}:
        path = write_html(graph, workspace)
        url = serve_graphs(path.parent)
        print(style.green(f"html  {path}"))
        print(style.green(f"http  {url}{path.name}"))
        print(style.dim("server stops when you quit wheel"))
        return
    print(render_ascii(graph), end="")


def handle_expand(spec: str = "") -> None:
    spec = (spec or "").strip()
    rec = SNIPS.get(spec)
    if rec is None:
        hint = f"try /expand {SNIPS.items[-1]['id']}" if SNIPS.items else "no tool output yet"
        print(style.dim(f"nothing to expand  ({hint})"))
        return
    _emit(style.prefix_block(f"full  {rec['id']}  {rec['name']}", rec["output"] or " ", style.cyan))
    FOOTER.paint()


def handle_compact(config: AgentConfig, workspace: Path, session: Session) -> None:
    if not session.items:
        print(style.dim("nothing to compact"))
        return
    if not provider_ready(config.provider):
        print(style.red("compact needs a provider API key"))
        return
    model = make_client(config.provider, effort=config.effort, cache_key=session.cache_key)
    before = len(session.items)
    try:
        compacted, extra, stats = compact_history(
            session.items,
            model,
            workspace,
            input_tokens=session.usage.input_tokens,
            context_window=config.provider.context_window,
            force=True,
            plan_text=session.plan.render() if session.plan.steps else "",
        )
    except Exception as exc:  # /refine guards this the same way: a provider hiccup must not crash the TUI
        print(style.red(f"compact failed: {exc}"))
        FOOTER.paint()
        return
    session.apply_compact(compacted)
    session.usage.add(extra)
    if stats.did:
        session.compactions += 1
        session.last_compact = stats.as_dict()
    session.persist(rewrite=True)
    if stats.did:
        print(
            style.green(
                f"compacted {stats.before_items} → {stats.after_items} items  "
                f"~{compact_count(stats.before_tokens)} → ~{compact_count(stats.after_tokens)} tok  "
                f"epoch {session.cache_epoch}"
            )
        )
    else:
        print(style.dim(f"nothing to compact ({before} items)"))
    FOOTER.set(_meter_text(config, session))


def _harness_store(workspace: Path, session: Session) -> HarnessStore:
    return HarnessStore.for_workspace(
        workspace,
        session_path=session.path,
        interactive=True,
    )


def handle_harness(workspace: Path, session: Session) -> None:
    store = _harness_store(workspace, session)
    listing = format_harness_for_prompt(store.merged(), max_content=None)
    _emit_clip("harness", "ok", listing, style.cyan)


def maybe_schedule_periodic_refine(config: AgentConfig, workspace: Path, session: Session) -> None:
    n = session.user_turns()
    last = _refine_at.get(session.session_id, 0)
    if not refine_due(n, AUTO_REFINE_EVERY, last):
        return
    _refine_at[session.session_id] = n
    schedule_auto_refine(config, workspace, session)


def schedule_auto_refine(config: AgentConfig, workspace: Path, session: Session) -> None:
    global _refine_thread
    with _refine_lock:
        if _refine_thread is not None and _refine_thread.is_alive():
            return
    items = [dict(item) for item in session.items]
    cache_key = session.cache_key

    def work() -> None:
        try:
            model = make_client(config.provider, effort="off", cache_key=cache_key)
            store = HarnessStore.for_workspace(
                workspace,
                session_path=session.path,
                interactive=True,
            )
            result, extra = run_refine(
                store,
                items,
                model,
                instructions=(
                    "Periodic refine after several user turns. "
                    "Extract only durable lessons. Skip one-off task progress."
                ),
                global_=False,
            )
            applied = [row for row in result.get("appliedEdits") or [] if row.get("applied")]
            payload = {
                "session": session,
                "usage": extra,
                "text": format_refine_result(result),
                "applied": bool(applied),
            }
        except Exception as exc:
            payload = {"session": session, "error": str(exc)}
        with _refine_lock:
            _refine_pending.append(payload)

    _refine_thread = threading.Thread(target=work, daemon=True, name="wheel-refine")
    _refine_thread.start()


def flush_auto_refine(config: AgentConfig, current: Session) -> bool:
    if _busy():
        return False
    with _refine_lock:
        batch = list(_refine_pending)
        _refine_pending.clear()
    if not batch:
        return False
    for item in batch:
        target = item.get("session") or current
        if item.get("error"):
            _emit(style.prefix_block("error  refine", str(item["error"]), style.red))
            continue
        target.usage.add(item["usage"])
        target.cache_epoch += 1
        target.persist(rewrite=True)
        label, paint = ("ok", style.green) if item.get("applied") else ("skip", style.dim)
        _emit_clip("refine", label, item["text"], paint)
        if target is current:
            FOOTER.set(_meter_text(config, current))
    FOOTER.paint()
    return True


def handle_refine_auto(rest: str) -> None:
    global AUTO_REFINE_EVERY
    spec = rest.strip().lower()
    if spec in {"", "status"}:
        if AUTO_REFINE_EVERY <= 0:
            print("auto-refine  off")
        else:
            print(f"auto-refine  every {AUTO_REFINE_EVERY} user turns  (background)")
        return
    if spec in {"off", "0", "false", "no"}:
        AUTO_REFINE_EVERY = 0
        print(style.dim("auto-refine off"))
        return
    if spec in {"on", "true", "yes"}:
        AUTO_REFINE_EVERY = 8
        print(style.green("auto-refine every 8 user turns"))
        return
    try:
        AUTO_REFINE_EVERY = max(0, int(spec))
    except ValueError:
        print("usage: /refine auto [N|off]")
        return
    if AUTO_REFINE_EVERY == 0:
        print(style.dim("auto-refine off"))
    else:
        print(style.green(f"auto-refine every {AUTO_REFINE_EVERY} user turns"))


def handle_jobs(rest: str = "") -> None:
    spec = rest.strip()
    if not spec:
        print(format_jobs())
        return
    command, _, target = spec.partition(" ")
    if command.lower() == "kill":
        target = target.strip()
        if not target:
            listing = format_jobs()
            if listing == "(no jobs)":
                print(style.dim("(no jobs)"))
                return
            options = listing.splitlines()
            picked = pick_list(options)
            if picked is None:
                return
            target = options[picked].split(None, 1)[0]
        try:
            print(kill_job(target))
        except ValueError as exc:
            print(style.red(str(exc)))
        return
    print("usage: /jobs | /jobs kill [id]")


def flush_jobs() -> bool:
    events = drain_job_events()
    if not events:
        return False
    for line in events:
        _emit(style.dim(line))
    FOOTER.paint()
    return True


def handle_refine(config: AgentConfig, workspace: Path, session: Session, rest: str) -> None:
    try:
        options = parse_refine_args(rest)
    except ValueError as exc:
        print(style.red(str(exc)))
        return
    if not session.items and not options.get("rollback_id"):
        print(style.dim("nothing to refine"))
        return
    if not provider_ready(config.provider):
        print(style.red("refine needs a provider API key"))
        return
    model = make_client(config.provider, effort="off", cache_key=session.cache_key)
    store = _harness_store(workspace, session)
    try:
        result, extra = run_refine(
            store,
            session.items,
            model,
            instructions=options.get("instructions"),
            rollback_id=options.get("rollback_id"),
            global_=bool(options.get("global")),
        )
    except Exception as exc:
        _emit(style.prefix_block("error  refine", str(exc), style.red))
        FOOTER.paint()
        return
    session.usage.add(extra)
    session.cache_epoch += 1
    session.persist(rewrite=True)
    applied = [row for row in result.get("appliedEdits") or [] if row.get("applied")]
    failed = [row for row in result.get("appliedEdits") or [] if not row.get("applied")]
    if failed and not applied:
        label, paint = "error", style.red
    elif failed:
        label, paint = "partial", style.yellow
    else:
        label, paint = "ok", style.green
    _emit_clip("refine", label, format_refine_result(result), paint)
    FOOTER.set(_meter_text(config, session))


def handle_undo(workspace: Path, spec: str = "") -> None:
    raw = spec.strip() or "1"
    try:
        n = int(raw)
    except ValueError:
        print(style.red("usage: /undo [n]"))
        return
    msgs = CheckpointStore.for_workspace(workspace).undo(n)
    if not msgs:
        print(style.dim("(nothing to undo)"))
        return
    for msg in msgs:
        print(style.green(msg))


def handle_undo_task(workspace: Path, task_id: str = "") -> None:
    store = CheckpointStore.for_workspace(workspace)
    msgs = store.rollback_task(task_id.strip() or None)
    if not msgs:
        print(style.dim("(nothing to undo for task)"))
        return
    print(style.green(f"rolled back task {task_id.strip() or 'latest'} ({len(msgs)} checkpoints)"))
    for msg in msgs:
        print(style.green(msg))


def session(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = False
    cleaned: list[str] = []
    for arg in argv:
        if arg in {"--json", "-j"}:
            json_mode = True
        else:
            cleaned.append(arg)
    argv = cleaned
    workspace = Path.cwd()
    global AUTO_REFINE_EVERY
    AUTO_REFINE_EVERY = parse_auto_refine_every()
    config = load_config(interactive=not json_mode)
    config.runs_dir = (workspace / config.runs_dir).resolve() if not config.runs_dir.is_absolute() else config.runs_dir
    trusted = ensure_project_trust(
        workspace,
        interactive=not json_mode,
        ask=ask_yes_no if not json_mode else None,
    )
    if json_mode:
        if not argv:
            sys.stderr.write("usage: wheel --json <task>\n")
            return 2
        return run_json_task(config, " ".join(argv), workspace)

    def begin_prompt() -> None:
        FOOTER.arm()
        FOOTER.paint()

    def print_chrome() -> int:
        cols = max(1, FOOTER._size()[1] - 1)
        lines: list[str] = []
        for line in style.banner().split("\n"):
            lines.extend(style.wrap_display(line, cols))
        window = compact_count(config.provider.context_window) if config.provider.context_window else "?"
        lines.extend(style.wrap_display(style.dim(f"workspace  {workspace}"), cols))
        context_files = load_project_files(workspace)
        if context_files:
            labels = []
            root = workspace.resolve()
            for path, _text in context_files:
                try:
                    labels.append(str(path.resolve().relative_to(root)))
                except ValueError:
                    labels.append(str(path))
            lines.extend(style.wrap_display(style.dim("context   " + ", ".join(labels)), cols))
        lines.extend(
            style.wrap_display(
                f"{style.bold('provider')}  {config.provider.name}  {style.cyan(config.provider.model)}  {style.dim(window)}",
                cols,
            )
        )
        lines.extend(style.wrap_display(style.dim(_effort_line(config)), cols))
        lines.extend(style.wrap_display(style.dim(f"session   {chat.session_id}"), cols))
        lines.extend(style.wrap_display(style.dim("任务直接回车；@文件 引用路径；多行粘贴整段提交；指令 /help  /quit；Tab 补全"), cols))
        for line in lines:
            style.writeln(line)
        return len(lines)

    def on_prompt_idle() -> bool:
        flushed = flush_auto_refine(config, chat)
        jobs = flush_jobs()
        return FOOTER.consume_resize() or flushed or jobs

    Session.purge_empty(workspace)
    chat = Session.create(workspace)
    ACTIVE["session"] = chat
    FOOTER.arm(reset=True)
    print_chrome()
    FOOTER.set(_meter_text(config, chat), cwd=str(workspace.resolve()))
    editor = LineEditor(
        _completion_words(config, workspace, trusted),
        on_idle=on_prompt_idle,
        on_paint=FOOTER.paint,
        at_files=lambda tok: list_at_files(workspace, tok),
        reserved_bottom=FOOTER.height,
    )
    busy_prompt = BusyPrompt(FOOTER)
    prev_winch = None
    if hasattr(signal, "SIGWINCH"):
        prev_winch = signal.signal(signal.SIGWINCH, lambda _signum, _frame: FOOTER.notify_resize())

    def abort_active() -> None:
        queue = ACTIVE.get("queue")
        runtime = ACTIVE.get("runtime")
        model = ACTIVE.get("model")
        if queue:
            queue.abort.set()
        if runtime is not None:
            runtime.abort_running()
        cancel = getattr(model, "cancel", None)
        if callable(cancel):
            cancel()

    def shutdown_ui() -> None:
        abort_active()
        stop_graph_server()
        if prev_winch is not None and hasattr(signal, "SIGWINCH"):
            signal.signal(signal.SIGWINCH, prev_winch)
        FOOTER.disarm()

    saw_interrupt = False

    def keep_after_interrupt() -> bool:
        """First Ctrl+C aborts the run and returns to `>`; second quits."""
        nonlocal saw_interrupt
        if _busy() and not saw_interrupt:
            saw_interrupt = True
            abort_active()
            # Unpin the `>` row so output resumes at the stream cursor; a plain
            # print while pinned lands on the input row and smears the footer.
            busy_prompt.hide()
            busy_prompt.buf = ""
            print(style.dim("\ninterrupted — Ctrl+C again to quit"))
            thread = ACTIVE.get("thread")
            if thread is not None:
                thread.join(timeout=2)
            return True
        shutdown_ui()
        print()
        return False

    def busy_wait() -> str | None:
        """Keep a pinned `>` while the worker streams. Echo stays off the say line."""
        tty_in = sys.stdin.isatty() and sys.stdout.isatty()
        if not tty_in:
            return _busy_wait_readline()
        import termios

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        prompt = busy_prompt
        try:
            enter_busy_tty(fd)
            sys.stdout.write("\033[?2004h")
            sys.stdout.flush()
            while _busy():
                FOOTER.consume_resize()
                queue = ACTIVE.get("queue")
                waiter = queue.pending_ask() if queue is not None else None
                if waiter is not None and not waiter._done.is_set():
                    prompt.hide()
                    sys.stdout.write("\033[?2004l")
                    sys.stdout.flush()
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    waiter.resolve(_ask_on_main(waiter.prompt))
                    enter_busy_tty(fd)
                    sys.stdout.write("\033[?2004h")
                    sys.stdout.flush()
                    prompt.show(query_cursor_row(fd))
                    continue
                key = _read_key(fd, timeout=0.12)
                if key is None:
                    continue
                if is_busy_abort_key(key):
                    raise KeyboardInterrupt
                if key == "\x04" and not prompt.buf:
                    raise EOFError
                line = prompt.feed(key)
                if line is not None and line.strip():
                    return line
            return None
        finally:
            sys.stdout.write("\033[?2004l")
            sys.stdout.flush()
            if not _busy():
                busy_prompt.hide()
                busy_prompt.buf = ""
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _busy_wait_readline() -> str | None:
        while _busy():
            FOOTER.consume_resize()
            queue = ACTIVE.get("queue")
            waiter = queue.pending_ask() if queue is not None else None
            if waiter is not None and not waiter._done.is_set():
                waiter.resolve(_ask_on_main(waiter.prompt))
                continue
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.15)
            except (ValueError, OSError):
                ready = []
            if ready:
                line = sys.stdin.readline()
                if line == "":
                    raise EOFError
                waiter = queue.pending_ask() if queue is not None else None
                if waiter is not None:
                    waiter.resolve(line.strip().lower() in {"y", "yes"})
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    continue
                return line
        return None

    def start_task(prompt: str) -> None:
        expanded = expand_skill_command(prompt, workspace, trusted=trusted)
        queue = TurnQueue()
        ACTIVE["queue"] = queue
        if sys.stdin.isatty() and sys.stdout.isatty():
            # Show the busy `>` and seed the stream cursor BEFORE the worker
            # writes anything: `── turn 1 ──` must continue right under the
            # committed task line (editor.last_cursor_row), not at the last
            # scroll row. A DSR query here would race the worker thread.
            busy_prompt.buf = ""
            busy_prompt.show(editor.last_cursor_row)

        def work() -> None:
            try:
                run_task(config, expanded, workspace, chat, queue=queue)
            except KeyboardInterrupt:
                pass
            except Exception as exc:
                print(style.red(f"agent error: {exc}"))
            finally:
                ACTIVE["thread"] = None
                ACTIVE["queue"] = None
                ACTIVE["runtime"] = None
                ACTIVE["model"] = None

        thread = threading.Thread(target=work, name="wheel-run", daemon=True)
        ACTIVE["thread"] = thread
        thread.start()

    def dispatch(line: str) -> bool:
        nonlocal config, chat
        text = line.strip()
        if _busy():
            queue: TurnQueue = ACTIVE["queue"]
            lowered = text.lower().lstrip("/")
            if lowered in {"stop", "quit", "exit", "q"}:
                abort_active()
                if lowered in {"quit", "exit", "q"}:
                    thread = ACTIVE.get("thread")
                    if thread:
                        thread.join(timeout=8)
                    return False
                _emit(style.dim("aborting…"))
                FOOTER.paint()
                return True
            if text.lower().startswith("/follow") or text.lower().startswith("follow "):
                payload = text.split(" ", 1)[1] if " " in text else ""
                payload = payload.lstrip(":").strip()
                queue.follow(payload)
                _emit(style.prefix_block("follow", payload or "(empty)", style.cyan))
                _emit(style.dim("follow… will run after this task would stop"))
                FOOTER.paint()
                return True
            if text.lower().startswith("/expand") or text.lower().startswith("expand "):
                handle_expand(text.split(" ", 1)[1] if " " in text.strip() else "")
                return True
            if text.startswith("/") and not text.startswith("/skill:"):
                print(style.dim("agent is running — Enter steers, /follow waits, /expand r3, /stop or Ctrl+C aborts"))
                return True
            payload = expand_skill_command(text, workspace, trusted=trusted) if text.startswith("/skill:") else text
            queue.steer(payload)
            _emit(style.prefix_block("steer", payload, style.cyan))
            _emit(style.dim("steering… next model step will see this"))
            FOOTER.paint()
            return True
        if text.startswith("/skill:"):
            start_task(text)
            return True
        slash = text.startswith("/")
        if slash:
            text = text[1:].strip()
            if not text:
                print(HELP)
                return True
        if not text:
            return True
        if text.lower() in {"quit", "exit", "q"}:
            return False
        if text.lower() in {"help", "?"}:
            print(HELP)
            return True
        command, _, rest = text.partition(" ")
        command = command.lower()
        if command == "provider":
            if not rest:
                names = list(config.providers)
                if not names:
                    print(style.dim("(no providers)"))
                    return True
                labels = [
                    f"{'*' if name == config.provider.name else ' '} {name:12} {config.providers[name].model}"
                    for name in names
                ]
                selected = names.index(config.provider.name) if config.provider.name in names else 0
                picked = pick_list(labels, selected)
                if picked is None:
                    return True
                rest = names[picked]
            try:
                config = config.with_provider(rest)
            except KeyError as exc:
                print(exc)
                return True
            print(style.green(f"switched to {config.provider.name} / {config.provider.model}"))
            print(_effort_line(config))
            editor.set_words(_completion_words(config, workspace, trusted))
            FOOTER.set(_meter_text(config, chat))
            return True
        if command in {"effort", "think"}:
            levels = list(_effort_choices(config))
            if not rest:
                if not levels:
                    print(style.dim("this model has no reasoning levels (set *_REASONING_LEVELS)"))
                    return True
                current = clamp_effort(config.effort, levels) or levels[0]
                selected = levels.index(current) if current in levels else 0
                picked = pick_list(levels, selected)
                if picked is None:
                    return True
                rest = levels[picked]
            want = normalize(rest)
            if want not in levels:
                known = " | ".join(levels) if levels else "(none)"
                print(f"unknown level {rest!r}; this model supports {known}")
                return True
            config = config.with_effort(want)
            print(_effort_line(config))
            return True
        if command in {"replay-session", "replay_session"}:
            handle_replay_session(config, chat, workspace, rest)
            return True
        if command == "replay":
            parts = rest.split()
            if parts and parts[0] == "session":
                handle_replay_session(config, chat, workspace, " ".join(parts[1:]))
                return True
            run_id = parts[0] if parts else ""
            execute = len(parts) > 1 and parts[1] == "go"
            if not run_id:
                runs = list_run_ids(config.runs_dir)
                if not runs:
                    print(style.dim("(no runs)"))
                    return True
                picked = pick_list(runs)
                if picked is None:
                    return True
                run_id = runs[picked]
            handle_replay(config, run_id, workspace, execute)
            return True
        if command == "expand":
            handle_expand(rest)
            return True
        if command == "compact":
            handle_compact(config, workspace, chat)
            return True
        if command == "undo":
            handle_undo(workspace, rest)
            return True
        if command in {"undo-task", "undo_task"}:
            handle_undo_task(workspace, rest)
            return True
        if command == "new":
            chat = Session.create(workspace)
            ACTIVE["session"] = chat
            print(style.green(f"new session {chat.session_id}"))
            _sync_plan_footer(busy=False, session=chat)
            FOOTER.set(_meter_text(config, chat))
            return True
        if command in {"sessions", "session"}:
            rows = Session.list_previews(workspace)
            if not rows:
                print(style.dim("(no sessions)"))
                return True
            for sid, preview in rows:
                mark = "*" if sid == chat.session_id else " "
                print(f"{mark} {sid}  {style.dim(preview)}")
            return True
        if command == "resume":
            chat = handle_resume(workspace, rest, chat)
            ACTIVE["session"] = chat
            _sync_plan_footer(busy=False, session=chat)
            FOOTER.set(_meter_text(config, chat))
            return True
        if command == "plan":
            print(chat.plan.render())
            return True
        if command == "harness":
            handle_harness(workspace, chat)
            return True
        if command == "jobs":
            handle_jobs(rest)
            return True
        if command == "refine":
            first, _, more = rest.strip().partition(" ")
            if first.lower() == "auto":
                handle_refine_auto(more)
                return True
            handle_refine(config, workspace, chat, rest)
            return True
        if command == "tree":
            return handle_tree(chat, rest)
        if command == "graph":
            handle_graph(chat, workspace, config.runs_dir, rest)
            return True
        if command == "fork":
            return handle_tree(chat, rest)
        if command == "follow":
            print(style.dim("no running task to follow"))
            return True
        if command == "stop":
            print(style.dim("nothing to stop"))
            return True
        if command in {"max-turns", "max_turns"} and not rest:
            print(f"max_turns  {config.max_turns}  (0=unlimited)")
            return True
        if command in {"max-turns", "max_turns"}:
            try:
                config = config.with_max_turns(int(rest))
            except ValueError as exc:
                print(exc)
                return True
            print(style.green(f"max_turns {config.max_turns}"))
            return True
        if slash:
            print(style.dim(f"unknown command  /{command}"))
            return True
        start_task(text)
        return True

    if argv:
        joined = " ".join(argv)
        if joined.startswith("/"):
            dispatch(joined)
        else:
            try:
                run_task(config, expand_skill_command(joined, workspace, trusted=trusted), workspace, chat)
            except KeyboardInterrupt:
                print(style.dim("\ninterrupted"))
        shutdown_ui()
        print()
        return 0

    while True:
        if not _busy():
            saw_interrupt = False
        try:
            if _busy():
                FOOTER.arm()
                FOOTER.paint()
                line = busy_wait()
                if line is None:
                    flush_auto_refine(config, chat)
                    flush_jobs()
                    continue
            else:
                flush_auto_refine(config, chat)
                flush_jobs()
                begin_prompt()
                line = editor.read()
                FOOTER.arm()
                FOOTER.paint()
        except EOFError:
            shutdown_ui()
            print()
            return 0
        except KeyboardInterrupt:
            if not keep_after_interrupt():
                return 0
            continue
        try:
            if not dispatch(line):
                shutdown_ui()
                print()
                return 0
        except KeyboardInterrupt:
            if not keep_after_interrupt():
                return 0
            continue
        FOOTER.paint()


def main() -> None:
    raise SystemExit(session())
