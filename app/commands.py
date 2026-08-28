"""Slash-command handlers: /resume, /tree, /graph, /compact, /harness,
/jobs, /undo, /replay and the session-replay destination helper.

Each handler is a thin adapter — parse args, do one thing, render with the
shared live-UI helpers (live.py). Re-exported from wheel_agent.app so the
REPL dispatch and the tests keep a single seam.
"""

from __future__ import annotations

import json
from pathlib import Path

from wheel_agent import style
from wheel_agent.app.state import STATE
from wheel_agent.app.live import _emit, _emit_clip, _meter_text, print_transcript
from wheel_agent.app.refine import _harness_store
from wheel_agent.checkpoint import CheckpointStore
from wheel_agent.compact import compact_history
from wheel_agent.config import AgentConfig, provider_ready
from wheel_agent.events import load_run
from wheel_agent.graph import build_session_graph, render_ascii, serve_graphs, write_html
from wheel_agent.harness import format_harness_for_prompt
from wheel_agent.meter import compact_count
from wheel_agent.model import make_client
from wheel_agent.repl import pick_list
from wheel_agent.replay import print_timeline, replay_run, replay_session
from wheel_agent.session import Session
from wheel_agent.tools import drain_job_events, format_jobs, kill_job


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
        STATE.footer.paint()
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
    STATE.footer.set(_meter_text(config, session))


def handle_harness(workspace: Path, session: Session) -> None:
    store = _harness_store(workspace, session)
    listing = format_harness_for_prompt(store.merged(), max_content=None)
    _emit_clip("harness", "ok", listing, style.cyan)


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
    STATE.footer.paint()
    return True


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


