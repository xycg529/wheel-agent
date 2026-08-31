"""Refine：从会话中提取持久经验到项目 harness。

两条入口共享同一个执行核心（_execute_refine）：

* manual  —— ``/refine [instructions] [--global] [--rollback <id>]`` 在 TUI 线程
  同步执行，内联报 ok/partial/error；
* auto    —— 每 N 个用户轮由后台线程 refine 会话快照并排队一个 payload；
  ``flush_auto_refine`` 在下一个空闲提示时打印它。

共享可变状态（节奏、待处理 payload、工作线程）在 app.state.STATE。"""

from __future__ import annotations

import threading

from wheel_agent.ui import style
from wheel_agent.ui.app.state import STATE
from wheel_agent.ui.app.live import _busy, _emit, _emit_clip, _meter_text
from wheel_agent.core.config import AgentConfig, provider_ready
from wheel_agent.harness.harness import HarnessStore
from wheel_agent.core.model import make_client
from wheel_agent.harness.refine import (
    format_refine_result,
    parse_refine_args,
    refine_due,
    run_refine,
)
from wheel_agent.core.session import Session


def _harness_store(workspace, session: Session) -> HarnessStore:
    """拿到当前工作区的 harness 存储（绑会话路径）。"""
    return HarnessStore.for_workspace(
        workspace,
        session_path=session.path,
        interactive=True,
    )


def _execute_refine(
    config: AgentConfig,
    workspace,
    session: Session,
    items,
    *,
    cache_key: str,
    instructions: str | None = None,
    rollback_id: str | None = None,
    global_: bool = False,
):
    """唯一构造 refine 模型调用 + harness 存储的地方。

    返回 run_refine 的 (result, extra_usage) 对；调用方决定怎么呈现
    （内联标签 vs 排队 payload）。推理档固定 off（只提取，不思考）。"""
    model = make_client(config.provider, effort="off", cache_key=cache_key)
    store = _harness_store(workspace, session)
    return run_refine(
        store,
        items,
        model,
        instructions=instructions,
        rollback_id=rollback_id,
        global_=global_,
    )


def maybe_schedule_periodic_refine(config: AgentConfig, workspace, session: Session) -> None:
    """每回合检查：到期了就后台排一次自动 refine。"""
    n = session.user_turns()
    last = STATE.refine_at.get(session.session_id, 0)
    if not refine_due(n, STATE.auto_refine_every, last):
        return
    STATE.refine_at[session.session_id] = n
    schedule_auto_refine(config, workspace, session)


def schedule_auto_refine(config: AgentConfig, workspace, session: Session) -> None:
    """起后台线程 refine 会话快照；结果排队等 flush。"""
    with STATE.refine_lock:
        if STATE.refine_thread is not None and STATE.refine_thread.is_alive():
            return   # 上一次还没跑完：跳过
    items = [dict(item) for item in session.items]   # 快照：后台读拷贝，不锁会话
    cache_key = session.cache_key

    def work() -> None:
        try:
            result, extra = _execute_refine(
                config,
                workspace,
                session,
                items,
                cache_key=cache_key,
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
        with STATE.refine_lock:
            STATE.refine_pending.append(payload)   # 排队；空闲时 flush 打印

    STATE.refine_thread = threading.Thread(target=work, daemon=True, name="wheel-refine")
    STATE.refine_thread.start()


def flush_auto_refine(config: AgentConfig, current: Session) -> bool:
    """空闲时把排队的 refine 结果打印出来；有打印返回 True。"""
    if _busy():
        return False
    with STATE.refine_lock:
        batch = list(STATE.refine_pending)
        STATE.refine_pending.clear()
    if not batch:
        return False
    for item in batch:
        target = item.get("session") or current
        if item.get("error"):
            _emit(style.prefix_block("error  refine", str(item["error"]), style.red))
            continue
        target.usage.add(item["usage"])
        target.invalidate_cache()   # harness 变了：下个任务重建上下文
        label, paint = ("ok", style.green) if item.get("applied") else ("skip", style.dim)
        _emit_clip("refine", label, item["text"], paint)
        if target is current:
            STATE.footer.set(_meter_text(config, current))
    STATE.footer.paint()
    return True


def handle_refine_auto(rest: str) -> None:
    """/refine auto [N|off]：查看/设置自动 refine 节奏。"""
    spec = rest.strip().lower()
    if spec in {"", "status"}:
        if STATE.auto_refine_every <= 0:
            print("auto-refine  off")
        else:
            print(f"auto-refine  every {STATE.auto_refine_every} user turns  (background)")
        return
    if spec in {"off", "0", "false", "no"}:
        STATE.auto_refine_every = 0
        print(style.dim("auto-refine off"))
        return
    if spec in {"on", "true", "yes"}:
        STATE.auto_refine_every = 8
        print(style.green("auto-refine every 8 user turns"))
        return
    try:
        STATE.auto_refine_every = max(0, int(spec))
    except ValueError:
        print("usage: /refine auto [N|off]")
        return
    if STATE.auto_refine_every == 0:
        print(style.dim("auto-refine off"))
    else:
        print(style.green(f"auto-refine every {STATE.auto_refine_every} user turns"))


def handle_refine(config: AgentConfig, workspace, session: Session, rest: str) -> None:
    """/refine [instructions] [--global] [--rollback <id>]：手动 refine。"""
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
    try:
        result, extra = _execute_refine(
            config,
            workspace,
            session,
            session.items,
            cache_key=session.cache_key,
            instructions=options.get("instructions"),
            rollback_id=options.get("rollback_id"),
            global_=bool(options.get("global")),
        )
    except Exception as exc:
        _emit(style.prefix_block("error  refine", str(exc), style.red))
        STATE.footer.paint()
        return
    session.usage.add(extra)
    session.invalidate_cache()   # harness 变了：下个任务重建上下文
    applied = [row for row in result.get("appliedEdits") or [] if row.get("applied")]
    failed = [row for row in result.get("appliedEdits") or [] if not row.get("applied")]
    if failed and not applied:
        label, paint = "error", style.red
    elif failed:
        label, paint = "partial", style.yellow
    else:
        label, paint = "ok", style.green
    _emit_clip("refine", label, format_refine_result(result), paint)
    STATE.footer.set(_meter_text(config, session))
