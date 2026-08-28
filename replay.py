from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from wheel_agent.config import AgentConfig, ProviderConfig
from wheel_agent.events import EventBus, load_run
from wheel_agent.graph import list_session_runs
from wheel_agent.loop import run_agent
from wheel_agent.model import ScriptedModel
from wheel_agent.types import RunResult


def print_timeline(bus: EventBus) -> str:
    lines: list[str] = []
    for event in bus.load_events():
        kind = event.get("type")
        if kind == "agent_start":
            lines.append(f"# {event.get('run_id')}  {event.get('provider')}/{event.get('model')}")
            lines.append(f"task: {event.get('task')}")
        elif kind == "turn_start":
            lines.append(f"\n== turn {event.get('turn')} ==")
        elif kind == "message_end":
            thinking = (event.get("thinking") or "").strip()
            if thinking:
                lines.append("[think]\n" + thinking)
            text = (event.get("text") or "").strip()
            if text:
                lines.append(text)
        elif kind == "tool_execution_start":
            args = json.dumps(event.get("args") or {}, ensure_ascii=False)
            lines.append(f"→ {event.get('tool_name')} {args}")
        elif kind == "tool_execution_end":
            flag = "ERR" if event.get("is_error") else "ok"
            if event.get("blocked"):
                flag = "BLOCK"
            preview = str(event.get("result") or "").replace("\n", " ")
            if len(preview) > 160:
                preview = preview[:157] + "..."
            lines.append(f"← {flag} {preview}")
        elif kind == "agent_end":
            lines.append(
                f"\nstop={event.get('stop_reason')} turns={event.get('turns')} "
                f"tokens={event.get('input_tokens')}+{event.get('output_tokens')}"
            )
        elif kind == "error":
            lines.append(f"ERROR {event.get('message')}")
    return "\n".join(lines).strip() + "\n"


def recorded_scripts(bus: EventBus) -> list[list[dict[str, Any]]]:
    return [row["output"] for row in bus.load_responses()]


def _events(bus: EventBus, kind: str) -> list[dict[str, Any]]:
    return [event for event in bus.load_events() if event.get("type") == kind]


def _tool_signature(bus: EventBus) -> list[dict[str, Any]]:
    starts = _events(bus, "tool_execution_start")
    ends = _events(bus, "tool_execution_end")
    signatures: list[dict[str, Any]] = []
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) else {}
        signatures.append(
            {
                "name": start.get("tool_name"),
                "args": start.get("args") or {},
                "decision": end.get("safety_decision") or "",
            }
        )
    return signatures


def _input_signature(bus: EventBus) -> list[dict[str, Any]]:
    return [row.get("input_audit") or {} for row in bus.load_responses()]


def _replay_status(source: EventBus, replayed: EventBus, target: RunResult) -> tuple[str, dict[str, Any]]:
    source_end = _events(source, "agent_end")[-1:]
    replay_end = _events(replayed, "agent_end")[-1:]
    if target.stop_reason in {"error", "api_error"} or not replay_end:
        return "error", {"reason": "replay did not reach agent_end"}
    source_fp = source_end[0].get("workspace_fingerprint") if source_end else None
    replay_fp = replay_end[0].get("workspace_fingerprint")
    tools_same = _tool_signature(source) == _tool_signature(replayed)
    source_inputs = _input_signature(source)
    replay_inputs = _input_signature(replayed)
    inputs_same = not any(source_inputs) or not any(replay_inputs) or source_inputs == replay_inputs
    stop_same = (source_end[0].get("stop_reason") if source_end else None) == target.stop_reason
    details = {
        "tools_same": tools_same,
        "inputs_same": inputs_same,
        "stop_same": stop_same,
        "source_workspace_fingerprint": source_fp,
        "replay_workspace_fingerprint": replay_fp,
    }
    if source_fp and replay_fp and source_fp != replay_fp:
        return "drift", details
    if tools_same and inputs_same and stop_same:
        return "exact", details
    return "behavioral", details


def replay_run(
    runs_dir: str | Path,
    run_id: str,
    workspace: str | Path,
    *,
    interactive: bool = False,
) -> tuple[str, RunResult]:
    source = load_run(runs_dir, run_id)
    timeline = print_timeline(source)
    scripts = recorded_scripts(source)
    meta = {}
    if source.meta_path.exists():
        meta = json.loads(source.meta_path.read_text(encoding="utf-8"))
    provider = ProviderConfig(
        name=str(meta.get("provider") or "replay"),
        api_key="",
        base_url=str(meta.get("base_url") or ""),
        model=str(meta.get("model") or "recorded"),
    )
    config = AgentConfig(
        provider=provider,
        providers={provider.name: provider},
        max_turns=int(meta.get("turns") or len(scripts) or 8) + 2,
        runs_dir=Path(runs_dir),
        interactive=interactive,
    )
    model = ScriptedModel(scripts)
    result = run_agent(
        task=str(meta.get("task") or ""),
        workspace=workspace,
        config=config,
        model=model,
        extra_meta={"replay_of": run_id, "mode": "replay"},
    )
    replay_bus = load_run(runs_dir, result.run_id)
    status, details = _replay_status(source, replay_bus, result)
    result.replay_status = status
    result.replay_details = details
    return timeline, result


_COPY_SKIP = {".wheel", ".wheel_runs", ".git", ".venv", "__pycache__", "node_modules"}


def copy_workspace(src: str | Path, dest: str | Path) -> Path:
    src = Path(src).resolve()
    dest = Path(dest).resolve()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        if child.name in _COPY_SKIP:
            continue
        target = dest / child.name
        if child.is_dir():
            shutil.copytree(child, target, ignore=shutil.ignore_patterns(*_COPY_SKIP))
        elif child.is_file():
            shutil.copy2(child, target)
    return dest


def replay_session(
    runs_dir: str | Path,
    session_id: str,
    workspace: str | Path,
    *,
    source_workspace: str | Path | None = None,
) -> list[RunResult]:
    run_ids = list_session_runs(session_id, runs_dir)
    if not run_ids:
        raise FileNotFoundError(f"no runs recorded for session {session_id}")
    dest = Path(workspace)
    if source_workspace is not None:
        copy_workspace(source_workspace, dest)
    dest.mkdir(parents=True, exist_ok=True)
    results: list[RunResult] = []
    for run_id in run_ids:
        _timeline, result = replay_run(runs_dir, run_id, dest, interactive=False)
        results.append(result)
    return results
