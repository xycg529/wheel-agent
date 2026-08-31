"""wheel 的 TUI 主入口：交互会话循环（session）+ main()。

负责：启动横幅、行编辑器接线、忙时 `>` 输入、斜杠命令分发（dispatch）、
Ctrl+C 两段式退出。具体渲染在 live.py，命令实现在 commands.py，
refine 在 refine.py，共享状态在 state.py。"""

from __future__ import annotations

import json
import select
import signal
import sys
import threading
from pathlib import Path

from wheel_agent.ui import style
from wheel_agent.core.config import AgentConfig, load_config, provider_ready
from wheel_agent.core.context import expand_skill_command, load_project_files, load_skills
from wheel_agent.tools.trust import ensure_project_trust
from wheel_agent.tools.atfiles import list_at_files
from wheel_agent.core.events import list_run_ids
from wheel_agent.ui.graph import stop_graph_server
from wheel_agent.core.loop import run_agent
from wheel_agent.core.meter import compact_count
from wheel_agent.core.model import make_client
from wheel_agent.core.queue import TurnQueue
from wheel_agent.core.reasoning import clamp_effort, normalize, reasoning_payload
from wheel_agent.ui.repl import BusyPrompt, LineEditor, _read_key, completion_words, enter_busy_tty, is_busy_abort_key, pick_list, query_cursor_row
from wheel_agent.harness.refine import parse_auto_refine_every
from wheel_agent.core.session import Session

# 为 main.py 和测试接缝重新导出（测试 import wheel_agent.ui.app 并摸
# app.print_transcript / app.ToolSnips / ...）；__all__ 让 pyflakes
# 知道哪些是有意公开的。
__all__ = [
    "LiveTurn",
    "STATE",
    "ToolSnips",
    "_busy",
    "_emit",
    "_format_args",
    "clip_tool_output",
    "handle_expand",
    "main",
    "print_event",
    "print_transcript",
    "tool_output_label",
]

from wheel_agent.ui.app.state import STATE
from wheel_agent.ui.app.commands import (
    flush_jobs,
    handle_compact,
    handle_graph,
    handle_harness,
    handle_jobs,
    handle_replay,
    handle_replay_session,
    handle_resume,
    handle_tree,
    handle_undo,
    handle_undo_task,
)
from wheel_agent.ui.app.refine import (
    flush_auto_refine,
    handle_refine,
    handle_refine_auto,
    maybe_schedule_periodic_refine,
)
from wheel_agent.ui.app.live import (
    LiveTurn,
    ToolSnips,
    _busy,
    _emit,
    _format_args,
    _meter_text,
    _sync_plan_footer,
    clip_tool_output,
    handle_expand,
    print_event,
    print_transcript,
    tool_output_label,
)





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


def _effort_line(config: AgentConfig) -> str:
    """当前推理档的单行文本（页脚用）。"""
    payload = reasoning_payload(config.effort, config.provider.effort_levels)
    level = payload["effort"] if payload else "off"
    return f"effort  {level}"


def _effort_choices(config: AgentConfig) -> tuple[str, ...]:
    """当前 provider 支持的推理档位。"""
    return tuple(config.provider.effort_levels)


def _completion_words(config: AgentConfig, workspace, trusted: bool) -> list[str]:
    """Tab 补全词表（命令 + provider + skill + 推理档）。"""
    return completion_words(
        config.providers,
        (s.name for s in load_skills(workspace, trusted=trusted)),
        effort_levels=_effort_choices(config),
    )


def ask_yes_no(prompt: str) -> bool:
    """向用户要 y/N：工作线程里则通过队列代理到主线程。"""
    queue = STATE.active.get("queue")
    if queue is not None and threading.current_thread() is not threading.main_thread():
        return queue.request_ask(prompt)
    return _ask_on_main(prompt)


def _ask_on_main(prompt: str) -> bool:
    """不用 input() 的 y/N：嵌套的 readline 会把下一个 > 提示藏掉。"""
    _emit(style.yellow(prompt))
    sys.stdout.write(style.dim("proceed? [y/N] "))
    sys.stdout.flush()
    try:
        line = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write("\n")
        sys.stdout.flush()
        STATE.footer.paint()
        return False
    # 把提示行收尾，否则下一个 ┌ ok / error 块会粘上来。
    sys.stdout.write("\n")
    sys.stdout.flush()
    STATE.footer.paint()
    if line == "":
        return False
    return line.strip().lower() in {"y", "yes"}


def _finish_session(session: Session, result) -> None:
    """任务结束后把轮次/用量写回会话并持久化。"""
    session.turn_offset += result.turns
    session.usage.add(result.usage)
    session.persist(rewrite=True)


def run_task(
    config: AgentConfig,
    task: str,
    workspace: Path,
    session: Session,
    queue: TurnQueue | None = None,
) -> None:
    """跑一个任务（交互模式）：建模型客户端、接事件/流式回调、跑 run_agent、收尾。"""
    if not provider_ready(config.provider):
        print(
            style.red(
                f"provider {config.provider.name} has no API key. "
                f"Set {config.provider.name.upper()}_API_KEY in .env"
            )
        )
        return
    STATE.live = LiveTurn()
    STATE.active["session"] = session
    session.plan.ask = ask_yes_no   # plan 工具的确认/交互走 UI 的 y/N
    session.plan.interactive = True
    _sync_plan_footer(busy=True, session=session)
    model = make_client(config.provider, effort=config.effort, cache_key=session.cache_key)
    if queue is not None:
        model.abort = queue.abort   # 中止信号接入模型调用
    STATE.active["model"] = model
    model.on_retry = lambda attempt, message: print_event(
        {"type": "api_retry", "attempt": attempt, "message": message}
    )

    def on_delta(kind: str, chunk: str) -> None:
        if queue is not None and queue.abort.is_set():
            return   # 已中止：丢后续增量
        STATE.live.on_delta(kind, chunk)

    def on_tool_update(chunk: str) -> None:
        if queue is not None and queue.abort.is_set():
            return
        STATE.live.on_tool_update(chunk)

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
        runtime_out=STATE.active,
    )
    STATE.live.close()
    STATE.active["last_task_id"] = result.task_id
    _finish_session(session, result)
    _sync_plan_footer(busy=False, session=session)
    STATE.footer.set(_meter_text(config, session, result.last_usage))
    if config.interactive:
        maybe_schedule_periodic_refine(config, workspace, session)   # 到期的自动 refine


def run_json_task(config: AgentConfig, task: str, workspace: Path) -> int:
    """--json 模式：跑一个任务，stdout 只出一行 JSON；按停止原因返回码。"""
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
    _finish_session(chat, result)
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
    return 0 if result.stop_reason in {"stop", "max_turns", "plan_rejected"} else 1   # 正常停=0，错误/超轮=1


def session(argv: list[str] | None = None) -> int:
    """主会话入口：配置加载、信任确认、REPL 主循环；返回退出码。"""
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
    STATE.auto_refine_every = parse_auto_refine_every()
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
        STATE.footer.arm()
        STATE.footer.paint()

    def print_chrome() -> int:
        """启动横幅：workspace/context/provider/session 信息；返回行数。"""
        cols = max(1, STATE.footer._size()[1] - 1)
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
        """行编辑器空闲时调用：有东西要打印（resize/refine/作业）返回 True。"""
        flushed = flush_auto_refine(config, chat)
        jobs = flush_jobs()
        return STATE.footer.consume_resize() or flushed or jobs

    Session.purge_empty(workspace)   # 清掉空会话，列表不垃圾堆积
    chat = Session.create(workspace)
    STATE.active["session"] = chat
    STATE.footer.arm(reset=True)
    print_chrome()
    STATE.footer.set(_meter_text(config, chat), cwd=str(workspace.resolve()))
    editor = LineEditor(
        _completion_words(config, workspace, trusted),
        on_idle=on_prompt_idle,
        on_paint=STATE.footer.paint,
        at_files=lambda tok: list_at_files(workspace, tok),   # @ 补全工作区文件
        reserved_bottom=STATE.footer.height,
    )
    busy_prompt = BusyPrompt(STATE.footer)
    prev_winch = None
    if hasattr(signal, "SIGWINCH"):
        prev_winch = signal.signal(signal.SIGWINCH, lambda _signum, _frame: STATE.footer.notify_resize())   # 窗口尺寸变化通知

    def abort_active() -> None:
        """三层中止：队列 abort 旗、运行时中止在跑的工具、模型客户端取消流。"""
        queue = STATE.active.get("queue")
        runtime = STATE.active.get("runtime")
        model = STATE.active.get("model")
        if queue:
            queue.abort.set()
        if runtime is not None:
            runtime.abort_running()
        cancel = getattr(model, "cancel", None)
        if callable(cancel):
            cancel()

    def shutdown_ui() -> None:
        """退出前清理：中止任务、关图服务、恢复 SIGWINCH、解除页脚固定。"""
        abort_active()
        stop_graph_server()
        if prev_winch is not None and hasattr(signal, "SIGWINCH"):
            signal.signal(signal.SIGWINCH, prev_winch)
        STATE.footer.disarm()

    saw_interrupt = False

    def keep_after_interrupt() -> bool:
        """第一次 Ctrl+C 中止运行并回到 `>`；第二次才退出。"""
        nonlocal saw_interrupt
        if _busy() and not saw_interrupt:
            saw_interrupt = True
            abort_active()
            # 解掉固定住的 `>` 行，让输出从流式光标继续；固定时普通 print
            # 会落在输入行上抹掉页脚。
            busy_prompt.hide()
            busy_prompt.buf = ""
            print(style.dim("\ninterrupted — Ctrl+C again to quit"))
            thread = STATE.active.get("thread")
            if thread is not None:
                thread.join(timeout=2)
            return True
        shutdown_ui()
        print()
        return False

    def busy_wait() -> str | None:
        """工作线程流式输出期间保持固定的 `>`；回显不进 say 行。"""
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
                STATE.footer.consume_resize()
                queue = STATE.active.get("queue")
                waiter = queue.pending_ask() if queue is not None else None
                if waiter is not None and not waiter._done.is_set():
                    # 工具在工作线程里问 y/N：临时回到主线程问完再继续忙等
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
        """非 TTY 的忙等：select 读 stdin，支持 y/N 应答和 steer 行。"""
        while _busy():
            STATE.footer.consume_resize()
            queue = STATE.active.get("queue")
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
        """起一个前台任务线程（工作线程跑 run_task）。"""
        expanded = expand_skill_command(prompt, workspace, trusted=trusted)
        queue = TurnQueue()
        STATE.active["queue"] = queue
        if sys.stdin.isatty() and sys.stdout.isatty():
            # 在工作线程写任何内容之前显示 busy `>` 并初始化流式光标：
            # `── turn 1 ──` 必须紧接已提交的任务行（editor.last_cursor_row），
            # 而不是最后一个滚动行。在这里查 DSR 会和工作线程竞态。
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
                STATE.active["thread"] = None
                STATE.active["queue"] = None
                STATE.active["runtime"] = None
                STATE.active["model"] = None   # 清句柄：下个任务重建

        thread = threading.Thread(target=work, name="wheel-run", daemon=True)
        STATE.active["thread"] = thread
        thread.start()

    def dispatch(line: str) -> bool:
        """分发一行输入；返回 False 表示退出。/ 开头才是命令，其余都是任务。"""
        nonlocal config, chat
        text = line.strip()
        if _busy():
            queue: TurnQueue = STATE.active["queue"]
            lowered = text.lower().lstrip("/")
            if text.startswith("/") and lowered in {"stop", "quit", "exit", "q"}:
                abort_active()
                if lowered in {"quit", "exit", "q"}:
                    thread = STATE.active.get("thread")
                    if thread:
                        thread.join(timeout=8)
                    return False
                _emit(style.dim("aborting…"))
                STATE.footer.paint()
                return True
            if text.lower().startswith("/follow"):
                payload = text.split(" ", 1)[1] if " " in text else ""
                payload = payload.lstrip(":").strip()
                queue.follow(payload)   # 排队：本轮正常停后再作为新任务投递
                _emit(style.prefix_block("follow", payload or "(empty)", style.cyan))
                _emit(style.dim("follow… will run after this task would stop"))
                STATE.footer.paint()
                return True
            if text.lower().startswith("/expand"):
                handle_expand(text.split(" ", 1)[1] if " " in text.strip() else "")
                return True
            if text.startswith("/") and not text.startswith("/skill:"):
                print(style.dim("agent is running — Enter steers, /follow waits, /expand r3, /stop or Ctrl+C aborts"))
                return True
            payload = expand_skill_command(text, workspace, trusted=trusted) if text.startswith("/skill:") else text
            queue.steer(payload)   # 下一轮模型调用就会看到
            _emit(style.prefix_block("steer", payload, style.cyan))
            _emit(style.dim("steering… next model step will see this"))
            STATE.footer.paint()
            return True
        if not text:
            return True
        if text.startswith("/skill:"):
            start_task(text)   # skill 注入始终当任务
            return True
        if not text.startswith("/"):
            # 只有 / 开头的输入是命令；其他一律当任务。
            start_task(text)
            return True
        text = text[1:].strip()
        if not text:
            print(HELP)
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
                config = config.with_provider(rest)   # 切 provider = 换模型通道
            except KeyError as exc:
                print(exc)
                return True
            print(style.green(f"switched to {config.provider.name} / {config.provider.model}"))
            print(_effort_line(config))
            editor.set_words(_completion_words(config, workspace, trusted))
            STATE.footer.set(_meter_text(config, chat))
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
            STATE.active["session"] = chat
            print(style.green(f"new session {chat.session_id}"))
            _sync_plan_footer(busy=False, session=chat)
            STATE.footer.set(_meter_text(config, chat))
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
            STATE.active["session"] = chat
            _sync_plan_footer(busy=False, session=chat)
            STATE.footer.set(_meter_text(config, chat))
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
        if command in {"tree", "fork"}:
            return handle_tree(chat, rest)
        if command == "graph":
            handle_graph(chat, workspace, config.runs_dir, rest)
            return True
        if command == "follow":
            print(style.dim("no running task to follow"))   # 空闲时 /follow 无意义
            return True
        if command == "stop":
            print(style.dim("nothing to stop"))
            return True
        if command in {"max-turns", "max_turns"}:
            if not rest:
                print(f"max_turns  {config.max_turns}  (0=unlimited)")
                return True
            try:
                config = config.with_max_turns(int(rest))
            except ValueError as exc:
                print(exc)
                return True
            print(style.green(f"max_turns {config.max_turns}"))
            return True
        print(style.dim(f"unknown command  /{command}"))   # 未知的 / 命令：提示但不退出
        return True

    if argv:
        # 命令行带参数：单任务模式（/ 开头走 dispatch，否则直接同步跑任务）
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
            saw_interrupt = False   # 回到空闲后重置两段式 Ctrl+C 计数
        try:
            if _busy():
                STATE.footer.arm()
                STATE.footer.paint()
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
                STATE.footer.arm()
                STATE.footer.paint()
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
        STATE.footer.paint()


def main() -> None:
    """python -m wheel_agent.ui.app 的入口。"""
    raise SystemExit(session())
