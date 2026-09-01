"""实时流式 UI：LiveTurn 状态机、事件打印、工具输出裁剪/摘存、
转录回放、页脚计量。

与命令处理（commands.py）和 refine 机制（refine.py）分开，
便于各自阅读和测试。进程级共享状态在 app.state.STATE。"""

from __future__ import annotations

import json
import sys

from wheel_agent.ui import style
from wheel_agent.ui.app.state import STATE
from wheel_agent.core.compact import SUMMARY_MARK, is_summary_item
from wheel_agent.core.config import AgentConfig
from wheel_agent.core.meter import compact_count, format_meter
from wheel_agent.ui.markdown import render_markdown
from wheel_agent.core.model import extract_text, extract_thinking, item_text
from wheel_agent.core.session import Session

# 工具输出默认显示上限（行/字符），超出部分摘存供 /expand。
UI_TOOL_LINES = 6
UI_TOOL_CHARS = 500


class ToolSnips:
    """被截断的工具输出仓库：给 /expand 按 id 取回全文。"""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self._n = 0

    def add(self, name: str, output: str, turn: object = None) -> dict:
        """存一条输出，返回记录（含分配到的 id rN）。"""
        self._n += 1
        rec = {"n": self._n, "id": f"r{self._n}", "name": name, "output": output, "turn": turn}
        self.items.append(rec)
        if len(self.items) > 40:
            self.items = self.items[-40:]   # 只留最近 40 条
        return rec

    def get(self, spec: str | int | None = None) -> dict | None:
        """按 spec 取记录：空/last=最后一条；r12 或 12=第 12 条。"""
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



class LiveTurn:
    """一个回合的流式渲染状态机：跟踪当前开着的 say/think/bash 块，
    增量写终端，结束时把块收口成完整帧（或整块替换已流式输出的行）。"""

    def __init__(self) -> None:
        self.text = False       # 本回合是否已出过 say
        self.thinking = False   # 是否已出过 think
        self.bash = False       # 是否正在跑工具
        self._open: str | None = None   # 当前开着的块：text/thinking/bash
        self._text = ""          # 流式中的 say 文本
        self._think = ""         # 流式中的 think 文本
        self._waiting = False    # 是否已打印 Working... 占位行

    def reset(self) -> None:
        self.close()
        self.text = False
        self.thinking = False
        self.bash = False
        self._text = ""
        self._think = ""

    def show_wait(self) -> None:
        """首包到达前打印 Working... 占位（只印一次）。"""
        if self._waiting or self._open or not style.is_tty():
            return
        # 提交完整行让它留在滚动区，而不是落在 `>` 行上。
        style.writeln(style.dim("Working..."))
        self._waiting = True

    def clear_wait(self) -> None:
        """首包到达时抹掉 Working... 占位。"""
        if not self._waiting:
            return
        self._waiting = False
        if style.is_tty():
            # writeln 把光标留在了下一行；擦掉那个空行和 Working... 行。
            style.replace_last_rows(2, "", reserved_bottom=STATE.footer.height())

    def close(self) -> None:
        """收口当前开着的块（text/thinking）。"""
        self.clear_wait()
        if self._open == "text":
            self.finish_say(self._text)
            return
        if self._open == "thinking":
            self.finish_think(self._think)
            return
        self._open = None

    def abandon_say(self) -> None:
        """关掉开着的 say/think 块，但不把它重印为完成的回复
        （比如 plan 被拒绝、或 hide_text 的内部消息）。"""
        self.clear_wait()
        if self._open in {"text", "thinking", "bash"}:
            style.writeln()
            style.writeln(style.dim("└"))
        self._open = None
        self._text = ""
        self.text = True

    def _finish_block(self, kind: str, body: str, empty: bool, label: str, paint=style.bold) -> None:
        """收口一个块：空内容擦掉；否则用完整帧替换流式行（或新输出）。"""
        buf = getattr(self, "_think" if kind == "thinking" else f"_{kind}")   # think 缓冲区叫 _think，不是 _thinking
        if empty:
            # 纯空白输出（比如推理模型在 think 和工具调用之间吐纯换行）：
            # 擦掉开着的块，不画帧。
            if self._open == kind and style.is_tty() and (kind == "text" or buf):
                style.replace_last_rows(style.open_block_rows(buf), "", reserved_bottom=STATE.footer.height())
            self._open = None
        else:
            rendered = style.frame(label, render_markdown(body), paint=paint)
            if self._open == kind and style.is_tty():
                style.replace_last_rows(style.open_block_rows(buf), rendered, reserved_bottom=STATE.footer.height())
            else:
                _emit(rendered)
            self._open = None
        setattr(self, kind, True)

    def finish_say(self, text: str) -> None:
        """收口 say 块。"""
        self._finish_block("text", text or self._text, not (text or self._text).strip(), "say")

    def finish_think(self, text: str) -> None:
        """收口 think 块（… / ... 视为空）。"""
        body = (text or self._think).strip()
        self._finish_block("thinking", body, body in {"", "…", "..."}, "think", style.magenta)

    def on_delta(self, kind: str, chunk: str) -> None:
        """流式增量到达（thinking/text）：切块、增量写终端。"""
        if kind == "thinking":
            if not chunk:
                return
            self.clear_wait()
            if self._open == "text":
                self.finish_say(self._text)   # 先收口 say 再开 think
            if self._open != "thinking":
                style.writeln(style.dim("┌ ") + style.magenta(style.bold("think")))
                self._open = "thinking"
            self._think += chunk
            style.stream_write(style.crlf(chunk) if "\n" in chunk else chunk)
            self.thinking = True
        elif kind == "text":
            self.clear_wait()
            if self._open == "thinking":
                self.finish_think(self._think)   # 先收口 think 再开 say
            self._text += chunk
            if not style.is_tty():
                return
            if self._open != "text":
                style.writeln(style.dim("┌ ") + style.bold("say"))
                self._open = "text"
            style.stream_write(style.crlf(chunk) if "\n" in chunk else chunk)
            self.text = True

    def on_tool_update(self, chunk: str) -> None:
        """工具开始执行：收口文字块，进入 bash 态（chunk 暂未使用）。"""
        del chunk
        self.clear_wait()
        if self._open == "text":
            self.finish_say(self._text)
        elif self._open == "thinking":
            self.finish_think(self._think)
        self.bash = True
        self._open = "bash"


def _meter_text(config: AgentConfig, session: Session, last=None) -> str:
    """页脚计量行文本（token/成本/压缩次数）。"""
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
    # 这里不能 STATE.footer.paint()：print 之间 CUP 到最后几行，
    # 会在横线折行时把相邻块粘在一起。
    style.writeln_wrapped(" ".join(str(a) for a in args) if args else "")


def parse_tool_args(raw: object) -> dict:
    """把工具参数解析成 dict（字符串参数先试 JSON）。"""
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
    """工具参数 → 人类可读的多行文本（长值截断）。"""
    parsed = dict(args) if isinstance(args, dict) else dict(parse_tool_args(args))
    if name == "ls" and not str(parsed.get("path") or "").strip():
        parsed["path"] = "."   # ls 缺省 path 显示为 .
    if not parsed:
        return ""
    lines = []
    for key, value in parsed.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if key in {"content", "new_string", "old_string"} and len(text) > 80:
            text = f"({len(text)} chars) {text[:60]}..."   # 编辑类长参数：只留头 60 字符
        elif len(text) > 120:
            text = text[:117] + "..."
        lines.append(f"{key}: {text}")
    return "\n".join(lines)


def tool_output_label(item: dict) -> tuple[str, object]:
    """根据 function_call_output 条目给（标签, 上色器）：blocked/error/ok。"""
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
    """截断工具输出；返回（显示体, 省略行数）。"""
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


def _emit_clip(name: str, label: str, output: str, paint, turn=None) -> None:
    """输出被截断的工具结果块（带 /expand 提示），并摘存全文。"""
    rec = STATE.snips.add(name, output, turn)
    body, omitted = clip_tool_output(output)
    if omitted:
        body = f"{body}\n/expand {rec['id']}"
    _emit(style.prefix_block(f"{label}  {rec['id']}  {name}", body or " ", paint))
    STATE.footer.paint()


def print_event(event: dict) -> None:
    """事件 → 终端输出（流式 UI 的主分发）。"""
    kind = event.get("type")
    queue = STATE.active.get("queue")
    if queue is not None and queue.abort.is_set() and kind not in {"error", "agent_end"}:
        return   # 已请求中止：除错误/结束外不再渲染
    live = STATE.live or LiveTurn()
    if kind == "turn_start":
        live.reset()
        STATE.live = live
        if event.get("user", True):
            label = event.get("display_turn") or event.get("turn")
            _emit()   # 空行分隔
            _emit(style.bold(style.cyan(f"── turn {label} ──")))
            sys.stdout.flush()
    elif kind == "message_start":
        STATE.live = live
        live.show_wait()
    elif kind == "message_end":
        thinking = (event.get("thinking") or "").strip()
        text = (event.get("text") or "").strip()
        if event.get("hide_text"):
            live.abandon_say()   # 内部消息：不收口显示
        elif live._open == "text":
            live.finish_say(text)
        elif live._open == "thinking":
            live.finish_think(thinking or live._think)
        else:
            live.close()
        if thinking and not live.thinking and not event.get("hide_text"):
            _emit(style.frame("think", render_markdown(thinking), paint=style.magenta))   # 非流式到达的 think 补帧
        if text and not live.text and not event.get("hide_text"):
            _emit(style.frame("say", render_markdown(text)))   # 非流式到达的 say 补帧
        STATE.footer.paint()
    elif kind == "plan_rejected":
        live.abandon_say()
        _emit()
        _emit(style.yellow("plan rejected — stopped. Next message revises the plan."))
        _sync_plan_footer(busy=False)   # 页脚 plan 状态同步回非 busy
        STATE.footer.paint()
    elif kind == "tool_execution_start":
        live.close()
        live.bash = False
        name = str(event.get("tool_name") or "tool")
        body = _format_args(event.get("args"), name)
        _emit(style.prefix_block(f"tool  {name}", body or " ", style.yellow))   # 工具调用块
    elif kind == "tool_execution_end":
        if event.get("blocked"):
            label, paint = "blocked", style.yellow
        elif event.get("is_error"):
            label, paint = "error", style.red
        else:
            label, paint = "ok", style.green
        output = str(event.get("result") or "")
        name = str(event.get("tool_name") or "tool")
        _emit_clip(name, label, output, paint, turn=event.get("turn"))
        live.bash = False
        live._open = None
        if name == "plan":
            _sync_plan_footer()   # plan 工具跑完：刷新页脚的 plan 行
        STATE.footer.paint()
    elif kind == "error":
        live.close()
        msg = str(event.get("message") or "error")
        _emit(style.red(msg))
        if event.get("transient"):
            _emit(style.dim("provider hiccup — session kept, send the task again or try another provider"))   # 瞬时错误：会话保留
    elif kind == "api_retry":
        live.close()
        _emit(style.dim(f"retry {event.get('attempt')} — {event.get('message')}"))
        live.show_wait()
    elif kind == "compact" and event.get("did"):
        live.close()
        before_t = compact_count(int(event.get("before_tokens") or 0))
        after_t = compact_count(int(event.get("after_tokens") or 0))
        _emit(   # 压缩完成提示：条目数/token/epoch 变化
            style.dim(
                f"compact  {event.get('before_items')} → {event.get('after_items')} items  "
                f"~{before_t} → ~{after_t} tok  epoch {event.get('epoch')}"
            )
        )
        STATE.footer.paint()


def _busy() -> bool:
    """前台线程是否在跑。"""
    thread = STATE.active.get("thread")
    return bool(thread and thread.is_alive())


def _sync_plan_footer(*, busy: bool | None = None, session: Session | None = None) -> None:
    """把当前 plan 的状态行同步进页脚。"""
    chat = session or STATE.active.get("session")
    plan = getattr(chat, "plan", None)
    if plan is None:
        runtime = STATE.active.get("runtime")
        plan = getattr(runtime, "plan", None)
    showing = _busy() if busy is None else busy
    lines = plan.footer_lines(busy=showing) if plan is not None else []
    STATE.footer.set_plan(lines)


def print_transcript(session: Session) -> None:
    """完整重印会话转录（/tree 跳转后、resume 后用）。"""
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
                _emit(style.prefix_block("summary", text.replace(SUMMARY_MARK, "").strip() or "compacted", style.dim))   # 压缩摘要
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
            label, paint = tool_output_label(item)
            _emit_clip(name, label, output, paint)
            continue
        think = extract_thinking([item])
        text = extract_text([item])
        if not text and role == "assistant":
            text = item_text(item)
        if think:
            _emit(style.frame("think", render_markdown(think), paint=style.magenta))
        if text:
            _emit(style.frame("say", render_markdown(text)))   # 普通 assistant 消息
    STATE.footer.paint()


def handle_expand(spec: str = "") -> None:
    """/expand：打印某条工具输出的全文。"""
    spec = (spec or "").strip()
    rec = STATE.snips.get(spec)
    if rec is None:
        hint = f"try /expand {STATE.snips.items[-1]['id']}" if STATE.snips.items else "no tool output yet"
        print(style.dim(f"nothing to expand  ({hint})"))
        return
    _emit(style.prefix_block(f"full  {rec['id']}  {rec['name']}", rec["output"] or " ", style.cyan))
    STATE.footer.paint()
