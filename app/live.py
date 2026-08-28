"""Live streaming UI: the LiveTurn state machine, event printing, tool-output
clipping/snipping, transcript replay, and footer metering.

Kept separate from the command handlers (commands.py) and the refine
machinery (refine.py) so each can be read and tested on its own. Shared
process state lives in app.state.STATE.
"""

from __future__ import annotations

import json
import sys

from wheel_agent import style
from wheel_agent.app.state import STATE
from wheel_agent.compact import SUMMARY_MARK, is_summary_item
from wheel_agent.config import AgentConfig
from wheel_agent.meter import compact_count, format_meter
from wheel_agent.markdown import render_markdown
from wheel_agent.model import extract_text, extract_thinking, item_text
from wheel_agent.session import Session

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




STATE.snips = ToolSnips()


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


STATE.snips = ToolSnips()



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
            style.replace_last_rows(2, "", reserved_bottom=STATE.footer.height())

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
                style.open_block_rows(self._text), rendered, reserved_bottom=STATE.footer.height()
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
                    style.open_block_rows(self._think), "", reserved_bottom=STATE.footer.height()
                )
            self._open = None
            self.thinking = True
            return
        rendered = style.frame("think", render_markdown(body), paint=style.magenta)
        if self._open == "thinking" and style.is_tty():
            style.replace_last_rows(
                style.open_block_rows(self._think), rendered, reserved_bottom=STATE.footer.height()
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
    # Do not STATE.footer.paint() here: CUP to the last rows between prints is what
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
    rec = STATE.snips.add(name, output)
    body, omitted = clip_tool_output(output)
    if omitted:
        body = f"{body}\n/expand {rec['id']}"
    _emit(style.prefix_block(f"{label}  {rec['id']}  {name}", body or " ", paint))
    STATE.footer.paint()


def print_event(event: dict) -> None:
    kind = event.get("type")
    queue = STATE.active.get("queue")
    if queue is not None and queue.abort.is_set() and kind not in {"error", "agent_end"}:
        return
    live = STATE.live or LiveTurn()
    if kind == "turn_start":
        live.reset()
        STATE.live = live
        if event.get("user", True):
            label = event.get("display_turn") or event.get("turn")
            _emit()
            _emit(style.bold(style.cyan(f"── turn {label} ──")))
            sys.stdout.flush()
    elif kind == "message_start":
        STATE.live = live
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
        STATE.footer.paint()
    elif kind == "plan_rejected":
        live.abandon_say()
        _emit()
        _emit(style.yellow("plan rejected — stopped. Next message revises the plan."))
        _sync_plan_footer(busy=False)
        STATE.footer.paint()
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
        rec = STATE.snips.add(name, output, event.get("turn"))
        body, omitted = clip_tool_output(output)
        if omitted:
            body = f"{body}\n/expand {rec['id']}"
        live.bash = False
        live._open = None
        _emit(style.prefix_block(f"{label}  {rec['id']}  {name}", body or " ", paint))
        if name == "plan":
            _sync_plan_footer()
        STATE.footer.paint()
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
        STATE.footer.paint()


def _busy() -> bool:
    thread = STATE.active.get("thread")
    return bool(thread and thread.is_alive())


def _sync_plan_footer(*, busy: bool | None = None, session: Session | None = None) -> None:
    chat = session or STATE.active.get("session")
    plan = getattr(chat, "plan", None)
    if plan is None:
        runtime = STATE.active.get("runtime")
        plan = getattr(runtime, "plan", None)
    showing = _busy() if busy is None else busy
    lines = plan.footer_lines(busy=showing) if plan is not None else []
    STATE.footer.set_plan(lines)


def print_transcript(session: Session) -> None:
    items = session.view_items()
    if not items:
        print(style.dim("(empty session)"))
        return
    STATE.snips.items.clear()
    STATE.snips._n = 0
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
            text = item_text(item)
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
            rec = STATE.snips.add(name, output)
            body, omitted = clip_tool_output(output)
            if omitted:
                body = f"{body}\n/expand {rec['id']}"
            label, paint = tool_output_label(item)
            _emit(style.prefix_block(f"{label}  {rec['id']}  {name}", body or " ", paint))
            continue
        think = extract_thinking([item])
        text = extract_text([item])
        if not text and role == "assistant":
            text = item_text(item)
        if think:
            _emit(style.frame("think", render_markdown(think), paint=style.magenta))
        if text:
            _emit(style.frame("say", render_markdown(text)))
    STATE.footer.paint()


def handle_expand(spec: str = "") -> None:
    spec = (spec or "").strip()
    rec = STATE.snips.get(spec)
    if rec is None:
        hint = f"try /expand {STATE.snips.items[-1]['id']}" if STATE.snips.items else "no tool output yet"
        print(style.dim(f"nothing to expand  ({hint})"))
        return
    _emit(style.prefix_block(f"full  {rec['id']}  {rec['name']}", rec["output"] or " ", style.cyan))
    STATE.footer.paint()
