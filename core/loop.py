"""Agent 主循环：ReAct 范式的 reason→act→observe 循环。

同一上下文里推理、调工具、看结果，直到模型不再调工具；
带紧凑、steer/follow 注入、y/N 询问、审计事件、错误归类。"""

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

# 回调签名：打印一行 / 流式增量（文本，思考标记）/ 工具进度。
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
    """跑一个任务直到结束，返回 RunResult。

    这是整个程序的心脏：组装工具/提示/安全门后进入主循环，
    每步调模型 → 无工具调用则收场（或消化排队输入），有则执行工具、
    把结果追加进上下文继续。steer 在两次调用之间注入，
    上下文溢出时强制紧凑后重试。"""
    ws = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
    bus = bus or EventBus.create(config.runs_dir)
    if on_event:
        bus.subscribe(on_event)
    if safety is None:
        # 默认安全门：把会话里已批准过的 bash 前缀载进记忆，跨轮免重复确认。
        memory = {tuple(row) for row in (session.approvals if session is not None else [])}
        safety = SafetyGate(interactive=config.interactive, ask=ask, memory=memory, workspace=ws.root)
    plan = plan or (session.plan if session is not None else PlanStore(ask=ask, interactive=config.interactive))
    store = HarnessStore.for_workspace(
        ws.root,
        session_path=session.path if session is not None else None,
        interactive=config.interactive,
    )
    runtime = ToolRuntime(ws, safety, on_update=on_tool_update, plan=plan, harness=store)
    task_id = runtime.begin_task()   # 开一个 checkpoint 任务，之后 /undo-task 用它
    initial_manifest = workspace_manifest(ws.root)
    initial_workspace_fingerprint = workspace_fingerprint(initial_manifest)
    if runtime_out is not None:
        runtime_out["runtime"] = runtime
        runtime_out["task_id"] = task_id
    tools = tool_schemas(list(runtime.tools.values()))
    # trusted：工作区可信（或没有项目级 skill 目录）才把项目 skill 注入系统提示。
    trusted = is_trusted(ws.root) or not project_skill_dirs(ws.root)
    instructions = system_prompt(ws.root, trusted=trusted, harness=store.merged())
    extra_input = ephemeral_items(ws.root, plan)   # 本轮临时上下文（日期/计划），不进历史
    if session is not None:
        items = session.items   # 别名：循环直接改 session 的视图列表
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
    # agent_start：记录环境/工作区指纹，replay 时用它判定环境是否漂移。
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
            # 入口检查 abort：/stop 在上一轮结束后才生效（不打断正在进行的模型调用）。
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
            turn = turn_offset + step   # turn 从会话的偏移续计，跨任务连续编号
            turns = step
            request_items = items + extra_input
            # 输入审计：每轮请求的指纹，replay 对比“输入是否一致”。
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
            # 模型调用（含上下文溢出时强制紧凑后重试一次）。
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
            # 记录模型原始响应：replay 用它把模型换成录制脚本重跑。
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
            # 调用 plan 工具时不回显正文：确认弹窗已经展示过计划了。
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
                # 无工具调用 = 本轮自然结束；但先看排队输入：
                # 用户敲的 steer/follow 还没发过模型，就作为新 user 消息补发。
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
            # 每个工具结果追加进上下文（模型下一步就能看到）；
            # write/edit 成功的路径记进 changed_files。
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
                # harness 笔记被改过：系统提示要重拼（笔记已变），
                # 同时自增缓存纪元——前缀变了，旧缓存不能再命中。
                store.dirty = False
                instructions = system_prompt(ws.root, trusted=trusted, harness=store.merged())
                if session is not None:
                    session.cache_epoch += 1
                    _sync_cache_key(model, session)
            if session is not None:
                session.approvals = [list(k) for k in safety.memory]
                extra_input = ephemeral_items(ws.root, plan)
                session.persist()
            bus.emit("turn_end", turn=turn, tool_calls=len(calls))
            # 计划被用户拒绝：这轮到此为止，等用户反馈后再改计划。
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
                # steer 注入：作为新 user 消息并入上下文，下一轮模型就能看到。
                bus.emit("steer_delivered", turn=turn, count=len(steers))
                user_turn = True
                display_turn += 1
            for msg in steers:
                _push(items, session, {"role": "user", "content": msg})
    except KeyboardInterrupt:
        # 运行中 Ctrl+C：中止后台 bash 作业，保留已完成内容，不抛异常。
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
        # 收尾时顺手紧凑：为下一个任务省输入 token。
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
        except Exception as exc:  # 宽抓：紧凑出错不能丢已完成的 run
            bus.emit("error", message=f"compact skipped: {exc}", transient=getattr(exc, "transient", False))

    final_manifest = workspace_manifest(ws.root)
    final_workspace_fingerprint = workspace_fingerprint(final_manifest)
    changes = workspace_changes(initial_manifest, final_manifest)   # 工作区改了哪些文件
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
    """追加一条消息到上下文（和会话树）。

    to_view=False：`items` 就是 session.items 本身（run_agent 起的别名）——
    上面的 append 已更新视图，树和文件只需补上新条目。"""
    items.append(item)
    if session is not None:
        session.append_item(item, to_view=False)
        session.persist()


def _sync_cache_key(model: ModelClient, session: Session | None) -> None:
    """把会话的缓存键同步到模型客户端（纪元一变，prompt_cache_key 跟着变）。"""
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
    """调一次模型；上下文溢出（各 provider 报错措辞不同）时强制紧凑后重试一次。"""
    extra = extra or []
    _sync_cache_key(model, session)
    try:
        return model.complete(items + extra, tools, instructions, on_delta=on_delta)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        if not compact or not is_context_overflow(exc):
            raise   # 不是溢出（或紧凑已关）：原样上抛
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
    """执行一批工具调用：前后各拍一次工作区快照，发 start/end 审计事件。"""
    before = workspace_manifest(runtime.workspace.root)
    before_fp = workspace_fingerprint(before)
    for call in calls:
        # 参数脱敏后发事件（密钥类字段不出现在日志里）。
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
