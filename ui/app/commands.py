"""斜杠命令处理器：/resume、/tree、/graph、/compact、/harness、
/jobs、/undo、/replay 以及 session 重放的目标目录辅助。

每个 handler 都是薄适配器——解析参数、做一件事、用共享的 live UI
辅助函数（live.py）渲染。从 wheel_agent.ui.app 重新导出，让 REPL 分发
和测试保持同一个接缝。"""

from __future__ import annotations

import json
from pathlib import Path

from wheel_agent.ui import style
from wheel_agent.ui.app.state import STATE
from wheel_agent.ui.app.live import _emit, _emit_clip, _meter_text, print_transcript
from wheel_agent.ui.app.refine import _harness_store
from wheel_agent.core.checkpoint import CheckpointStore
from wheel_agent.core.compact import compact_history
from wheel_agent.core.config import AgentConfig, provider_ready
from wheel_agent.core.events import load_run
from wheel_agent.ui.graph import build_session_graph, render_ascii, serve_graphs, write_html
from wheel_agent.harness.harness import format_harness_for_prompt
from wheel_agent.core.meter import compact_count
from wheel_agent.core.model import make_client
from wheel_agent.ui.repl import pick_list
from wheel_agent.ui.replay import print_timeline, replay_run, replay_session
from wheel_agent.core.session import Session
from wheel_agent.tools.tools import drain_job_events, format_jobs, kill_job


def handle_replay_session(config: AgentConfig, session: Session, workspace: Path, dest_spec: str = "") -> None:
    """/replay session：按顺序重放整个 session，默认落 .wheel/session-replay/<id>。"""
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
    """/replay [id] [go]：打印时间线；带 go 时重放一次。"""
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


def handle_resume(workspace: Path, rest: str, current: Session) -> Session:
    """/resume [id]：带 id 直接恢复，不带则用选择器挑；重印转录。"""
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
    """tree 的一行：* 标记路径上的节点，缩进表示深度。"""
    mark = "*" if row["on_path"] else " "
    indent = "  " * int(row["depth"])
    return f"{mark} {indent}{row['id']}  {row['label']}"


def handle_tree(session: Session, target: str | None = "", *, jumping: bool = False) -> bool:
    """/tree [id]：列出会话树；带 id（或选择器选中）则跳转/fork。"""
    spec = (target or "").strip()
    rows = session.tree_rows()
    if not spec and not jumping:
        if not rows:
            print(style.dim("(empty)"))
            return True
        labels = [_tree_option(row) for row in rows]
        selected = next((i for i, row in enumerate(rows) if row["leaf"]), 0)   # 默认选中当前叶子
        picked = pick_list(labels, selected)
        if picked is None:
            return True
        spec = rows[picked]["id"]
    if spec or jumping:
        try:
            session.fork(spec or None)   # 跳转 = 移动叶子指针（零拷贝）
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
    """/graph：ASCII 图；带 html 时写文件并起本地 HTTP 服务。"""
    graph = build_session_graph(session, runs_dir)
    if not graph.layers and graph.tree.empty():
        print(style.dim("(empty session)"))
        return
    if rest.strip().lower() in {"html", "open", "web", "serve"}:
        path = write_html(graph, workspace)
        url = serve_graphs(path.parent)
        print(style.green(f"html  {path}"))
        print(style.green(f"http  {url}{path.name}"))
        print(style.dim("server stops when you quit wheel"))   # 服务随进程退出关闭
        return
    print(render_ascii(graph), end="")


def handle_compact(config: AgentConfig, workspace: Path, session: Session) -> None:
    """/compact：立即压缩当前会话历史（force=True）。"""
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
    except Exception as exc:  # /refine 同样这么保护：provider 抖动不能搞崩 TUI
        print(style.red(f"compact failed: {exc}"))
        STATE.footer.paint()
        return
    session.apply_compact(compacted)
    session.usage.add(extra)
    if stats.did:
        session.compactions += 1
        session.last_compact = stats.as_dict()
    session.persist(rewrite=True)   # 压缩改了前缀：必须重写历史文件并 bump epoch
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
    STATE.footer.set(_meter_text(config, session))


def handle_harness(workspace: Path, session: Session) -> None:
    """/harness：打印当前 harness（notes+memories）的内容。"""
    store = _harness_store(workspace, session)
    listing = format_harness_for_prompt(store.merged(), max_content=None)
    _emit_clip("harness", "ok", listing, style.cyan)


def handle_jobs(rest: str = "") -> None:
    """/jobs：列出后台 bash 作业；/jobs kill [id] 杀掉一个（不带 id 用选择器）。"""
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
    """有空时把后台作业的待输出事件打印出来；有打印返回 True。"""
    events = drain_job_events()
    if not events:
        return False
    for line in events:
        _emit(style.dim(line))
    STATE.footer.paint()
    return True


def handle_undo(workspace: Path, spec: str = "") -> None:
    """/undo [n]：撤销最近 n 个 write/edit。"""
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
    """/undo-task [id]：回滚整个 task 的文件改动（默认最近一个 task）。"""
    store = CheckpointStore.for_workspace(workspace)
    msgs = store.rollback_task(task_id.strip() or None)
    if not msgs:
        print(style.dim("(nothing to undo for task)"))
        return
    print(style.green(f"rolled back task {task_id.strip() or 'latest'} ({len(msgs)} checkpoints)"))
    for msg in msgs:
        print(style.green(msg))


