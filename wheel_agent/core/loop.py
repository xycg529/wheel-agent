from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from wheel_agent.tools.audit import (
    environment_fingerprint,
    item_audit,
    redact_tool_args,
    redact_tool_output,
    tool_audit,
    workspace_changes,
    workspace_fingerprint,
    workspace_manifest,
)
from wheel_agent.core.compact import compact_history, is_context_overflow
from wheel_agent.core.config import AgentConfig
from wheel_agent.core.events import EventBus
from wheel_agent.core.model import ModelClient, extract_text, extract_thinking
from wheel_agent.core.plan import PlanStore
from wheel_agent.harness.harness import HarnessStore
from wheel_agent.core.prompt import ephemeral_items, system_prompt
from wheel_agent.tools.trust import is_trusted, project_skill_dirs
from wheel_agent.core.queue import TurnQueue
from wheel_agent.core.reasoning import clamp_effort, reasoning_payload
from wheel_agent.tools.safety import SafetyGate
from wheel_agent.core.session import Session
from wheel_agent.tools.tools import ToolRuntime, parse_function_calls, tool_schemas
from wheel_agent.core.types import APIError, FunctionCall, RunResult, ToolResult, Usage
from wheel_agent.tools.workspace import Workspace

Printer = Callable[[str], None]
DeltaFn = Callable[[str, str], None]
ToolUpdateFn = Callable[[str], None]


def run_agent(
    task: str,
    workspace: str | Path,
    config: AgentConfig,
    model: ModelClient,
    *,
    bus: EventBus | None = None,
    safety: SafetyGate | None = None,
    ask: Callable[[str], bool] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    on_delta: DeltaFn | None = None,
    on_tool_update: ToolUpdateFn | None = None,
    extra_meta: dict[str, Any] | None = None,
    items: list[dict[str, Any]] | None = None,
    turn_offset: int = 0,
    compact: bool = True,
    queue: TurnQueue | None = None,
    session: Session | None = None,
    plan: PlanStore | None = None,
    runtime_out: dict[str, Any] | None = None,
) -> RunResult:
    ws = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
    bus = bus or EventBus.create(config.runs_dir)
    if on_event:
        bus.subscribe(on_event)
    if safety is None:
        memory = {tuple(row) for row in (session.approvals if session is not None else [])}
        safety = SafetyGate(interactive=config.interactive, ask=ask, memory=memory, workspace=ws.root)
    plan = plan or (session.plan if session is not None else PlanStore(ask=ask, interactive=config.interactive))
    store = HarnessStore.for_workspace(
        ws.root,
        session_path=session.path if session is not None else None,
        interactive=config.interactive,
    )
    runtime = ToolRuntime(ws, safety, on_update=on_tool_update, plan=plan, harness=store)
    task_id = runtime.begin_task()
    initial_manifest = workspace_manifest(ws.root)
    initial_workspace_fingerprint = workspace_fingerprint(initial_manifest)
    if runtime_out is not None:
        runtime_out["runtime"] = runtime
        runtime_out["task_id"] = task_id
    tools = tool_schemas(list(runtime.tools.values()))
    trusted = is_trusted(ws.root) or not project_skill_dirs(ws.root)
    instructions = system_prompt(ws.root, trusted=trusted, harness=store.merged())
    extra_input = ephemeral_items(ws.root, plan)
    if session is not None:
        items = session.items
    else:
        items = list(items) if items is not None else []
    _push(items, session, {"role": "user", "content": task})
    usage = Usage()
    last_usage = Usage()
    tool_results: list[ToolResult] = []
    changed_files: list[str] = []
    final_text = ""
    stop_reason = "stop"
    turns = 0

    clamped = clamp_effort(config.effort, config.provider.effort_levels)
    bus.emit(
        "agent_start",
        workspace=str(ws.root),
        task=task,
        task_id=task_id,
        workspace_fingerprint=initial_workspace_fingerprint,
        environment_fingerprint=environment_fingerprint(),
        provider=config.provider.name,
        model=config.provider.model,
        effort=config.effort,
        effort_clamped=clamped,
        reasoning=reasoning_payload(config.effort, config.provider.effort_levels),
    )
    unlimited = config.max_turns <= 0
    try:
        step = 0
        user_turn = True
        display_turn = session.user_turns() if session is not None else 1
        while True:
            if queue and queue.abort.is_set():
                stop_reason = "aborted"
                final_text = final_text or "interrupted"
                bus.emit("error", message="interrupted")
                break
            step += 1
            if not unlimited and step > config.max_turns:
                stop_reason = "max_turns"
                final_text = final_text or f"Stopped after {config.max_turns} turns."
                break
            turn = turn_offset + step
            turns = step
            request_items = items + extra_input
            input_audit = item_audit(request_items, ws.root)
            input_audit["workspace_fingerprint"] = workspace_fingerprint(workspace_manifest(ws.root))
            bus.emit(
                "turn_start",
                turn=turn,
                step=step,
                user=user_turn,
                display_turn=display_turn,
                input_audit=input_audit,
                environment_fingerprint=environment_fingerprint(),
            )
            bus.emit("message_start", turn=turn)
            user_turn = False
            response = _complete_with_overflow(
                model,
                items,
                tools,
                instructions,
                on_delta=on_delta,
                workspace=ws.root,
                context_window=config.provider.context_window,
                compact=compact,
                session=session,
                extra=extra_input,
            )
            if queue and queue.abort.is_set():
                stop_reason = "aborted"
                final_text = extract_text(response.output) or final_text or "interrupted"
                bus.emit("error", message="interrupted")
                break
            usage.add(response.usage)
            last_usage = response.usage
            bus.record_response(
                turn,
                response.output,
                {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
                input_audit=input_audit,
            )
            for item in response.output:
                _push(items, session, item)
            extra_input = ephemeral_items(ws.root, plan)
            text = extract_text(response.output)
            thinking = extract_thinking(response.output)
            if text:
                final_text = text
            calls = parse_function_calls(response.output)
            hide_text = any(call.name == "plan" for call in calls)
            bus.emit(
                "message_end",
                turn=turn,
                text="" if hide_text else text,
                thinking=thinking,
                response_id=response.raw_id,
                streamed=bool(on_delta),
                hide_text=hide_text,
            )
            if not calls:
                pending = []
                if queue:
                    pending.extend(queue.drain_steer())
                    pending.extend(queue.drain_follow())
                bus.emit("turn_end", turn=turn, tool_calls=0, queued_inputs=len(pending))
                if pending:
                    for msg in pending:
                        _push(items, session, {"role": "user", "content": msg})
                    user_turn = True
                    display_turn += 1
                    continue
                stop_reason = "stop"
                break
            batch = _run_tools(runtime, calls, bus, turn)
            tool_results.extend(batch)
            for call, result in zip(calls, batch):
                _push(
                    items,
                    session,
                    {
                        "type": "function_call_output",
                        "call_id": result.call_id,
                        "output": result.output,
                        "is_error": bool(result.is_error),
                        "blocked": bool(result.blocked),
                    },
                )
                if result.name in {"write", "edit"} and not result.is_error:
                    path = str(call.arguments.get("path") or "")
                    if path and path not in changed_files:
                        changed_files.append(path)
            if store.dirty:
                store.dirty = False
                instructions = system_prompt(ws.root, trusted=trusted, harness=store.merged())
                if session is not None:
                    session.cache_epoch += 1
                    if hasattr(model, "cache_key"):
                        model.cache_key = session.cache_key
            if session is not None:
                session.approvals = [list(k) for k in safety.memory]
                extra_input = ephemeral_items(ws.root, plan)
                session.persist()
            bus.emit("turn_end", turn=turn, tool_calls=len(calls))
            if any(item.name == "plan" and item.is_error and "rejected" in item.output.lower() for item in batch):
                stop_reason = "plan_rejected"
                final_text = final_text or "plan rejected"
                bus.emit("plan_rejected", turn=turn)
                break
            if queue and queue.abort.is_set():
                stop_reason = "aborted"
                final_text = final_text or "interrupted"
                bus.emit("error", message="interrupted")
                break
            steers = queue.drain_steer() if queue else []
            if steers:
                bus.emit("steer_delivered", turn=turn, count=len(steers))
                user_turn = True
                display_turn += 1
            for msg in steers:
                _push(items, session, {"role": "user", "content": msg})
    except KeyboardInterrupt:
        runtime.abort_running()
        stop_reason = "aborted"
        final_text = final_text or "interrupted"
        bus.emit("error", message="interrupted")
        if queue:
            queue.abort.set()
        else:
            raise
    except APIError as exc:
        stop_reason = "api_error"
        final_text = str(exc)
        bus.emit("error", message=str(exc), transient=exc.transient, status=exc.status)
    except Exception as exc:
        stop_reason = "error"
        final_text = f"agent error: {exc}"
        bus.emit("error", message=str(exc))

    if compact and stop_reason in {"stop", "max_turns"}:
        try:
            compacted, extra, stats = compact_history(
                items,
                model,
                ws.root,
                input_tokens=last_usage.input_tokens,
                context_window=config.provider.context_window,
                plan_text=plan.render() if plan.steps else "",
            )
            usage.add(extra)
            if compacted is not items:
                items[:] = compacted
            if session is not None:
                session.apply_compact(items)
                if stats.did:
                    session.compactions += 1
                    session.last_compact = stats.as_dict()
            if stats.did:
                bus.emit(
                    "compact",
                    **stats.as_dict(),
                    epoch=session.cache_epoch if session is not None else 0,
                )
        except Exception as exc:  # broad: a compact hiccup must never discard a finished run
            bus.emit("error", message=f"compact skipped: {exc}", transient=getattr(exc, "transient", False))

    final_manifest = workspace_manifest(ws.root)
    final_workspace_fingerprint = workspace_fingerprint(final_manifest)
    changes = workspace_changes(initial_manifest, final_manifest)
    bus.emit(
        "agent_end",
        turns=turns,
        stop_reason=stop_reason,
        text=final_text,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=usage.cached_tokens,
        task_id=task_id,
        workspace_fingerprint=final_workspace_fingerprint,
        workspace_changes=changes,
    )
    meta = {
        "workspace": str(ws.root),
        "task": task,
        "task_id": task_id,
        "initial_workspace_fingerprint": initial_workspace_fingerprint,
        "workspace_fingerprint": final_workspace_fingerprint,
        "workspace_changes": changes,
        "environment_fingerprint": environment_fingerprint(),
        "provider": config.provider.name,
        "model": config.provider.model,
        "base_url": config.provider.base_url,
        "api": config.provider.api,
        "effort": config.effort,
        "effort_clamped": clamped,
        "effort_levels": list(config.provider.effort_levels),
        "turns": turns,
        "stop_reason": stop_reason,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_tokens": usage.cached_tokens,
        "tool_calls": len(tool_results),
        "tool_errors": sum(1 for item in tool_results if item.is_error),
    }
    if extra_meta:
        meta.update(extra_meta)
    bus.write_meta(**meta)
    if session is not None:
        session.approvals = [list(k) for k in safety.memory]
    return RunResult(
        run_id=bus.run_id,
        text=final_text,
        turns=turns,
        usage=usage,
        tool_results=tool_results,
        stop_reason=stop_reason,
        events_path=str(bus.events_path),
        items=items,
        last_usage=last_usage,
        changed_files=changed_files,
        task_id=task_id,
    )


def _push(items: list[dict[str, Any]], session: Session | None, item: dict[str, Any]) -> None:
    items.append(item)
    if session is not None:
        # to_view=False: `items` is session.items itself (aliased by
        # run_agent) — the append above already updated the view; only the
        # tree + file need the new entry.
        session.append_item(item, to_view=False)
        session.persist()


def _sync_cache_key(model: ModelClient, session: Session | None) -> None:
    if session is not None and hasattr(model, "cache_key"):
        model.cache_key = session.cache_key


def _complete_with_overflow(
    model: ModelClient,
    items: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    instructions: str,
    *,
    on_delta: DeltaFn | None,
    workspace: Path,
    context_window: int,
    compact: bool,
    session: Session | None = None,
    extra: list[dict[str, Any]] | None = None,
):
    extra = extra or []
    _sync_cache_key(model, session)
    try:
        return model.complete(items + extra, tools, instructions, on_delta=on_delta)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        if not compact or not is_context_overflow(exc):
            raise
        compacted, _extra, stats = compact_history(
            items,
            model,
            workspace,
            input_tokens=context_window,
            context_window=context_window,
            force=True,
        )
        items[:] = compacted
        if session is not None:
            session.apply_compact(items)
            if stats.did:
                session.compactions += 1
                session.last_compact = stats.as_dict()
            session.persist(rewrite=True)
        _sync_cache_key(model, session)
        return model.complete(items + extra, tools, instructions, on_delta=on_delta)


def _run_tools(runtime: ToolRuntime, calls: list[FunctionCall], bus: EventBus, turn: int) -> list[ToolResult]:
    before = workspace_manifest(runtime.workspace.root)
    before_fp = workspace_fingerprint(before)
    for call in calls:
        bus.emit(
            "tool_execution_start",
            turn=turn,
            tool_call_id=call.call_id,
            tool_name=call.name,
            args=redact_tool_args(call.name, call.arguments),
            args_sha256=tool_audit(call, ToolResult(call.call_id, call.name, ""))["args_sha256"],
            workspace_fingerprint=before_fp,
        )
    results = runtime.execute_batch(calls)
    after = workspace_manifest(runtime.workspace.root)
    after_fp = workspace_fingerprint(after)
    changes = workspace_changes(before, after)
    for call, result in zip(calls, results):
        audit = tool_audit(call, result)
        bus.emit(
            "tool_execution_end",
            turn=turn,
            tool_call_id=result.call_id,
            tool_name=result.name,
            is_error=result.is_error,
            blocked=result.blocked,
            result=redact_tool_output(call.name, call.arguments, result.output),
            safety_decision=audit["decision"],
            safety_reason=audit["decision_reason"],
            safety_source=audit["decision_source"],
            workspace_fingerprint=after_fp,
            workspace_changes=changes,
        )
    return results
