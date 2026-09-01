"""系统提示与每轮 ephemeral 上下文：把工作区规则、skills、harness、计划
拼进 system 提示，或作为本轮临时 system 消息。"""

from __future__ import annotations

from pathlib import Path

from wheel_agent.core.context import format_project_xml, format_skills_xml, load_project_files, load_skills, today
from wheel_agent.harness.harness import HarnessState, format_harness_for_prompt
from wheel_agent.core.plan import PlanStore

# 每轮临时上下文的开头标记：告诉模型这不是用户消息。
EPHEMERAL_MARK = "[ephemeral context — not a user message]"


def system_prompt(
    workspace: str | Path,
    home: str | Path | None = None,
    *,
    trusted: bool = True,
    harness: HarnessState | None = None,
) -> str:
    """组装系统提示：agent 行为约定 + 项目指令 + skills + harness 持久化笔记。

    文本里嵌着对模型的关键约束（bash 超时/后台任务、plan 工具的使用纪律、
    破坏性命令必须先过 harness），改动这些句子会影响模型行为。"""
    root = Path(workspace).resolve()
    parts = [
        "You are a coding agent. Work only inside the workspace. "
        f"Workspace: {root}\n"
        "Tools: read, ls, glob, grep, write, edit, bash, bash_poll, bash_kill, plan, harness, web_search, web_fetch.\n"
        "ls lists one directory. glob finds file paths by name pattern (ripgrep --files). "
        "grep searches file contents; pass glob= to filter filenames.\n"
        "Edit with unique old_string/new_string. If not unique, set replace_all or add more context.\n"
        "bash foreground timeout is 120s then the process is killed. "
        "Install, test suites, and servers MUST use bash with background=true. "
        "That tool returns a job_id immediately — tell the user the job_id and STOP this turn. "
        "Do not bash_poll in a loop here; the user will keep chatting and can /jobs or ask later.\n"
        "When the user asks for a plan / 计划 / 分步 (even if they say do not edit files), "
        "explore if needed, then you MUST call the plan tool with the full step list. "
        "The harness asks the user y/N on the first plan tool call. "
        "If the tool result says plan approved, the user already confirmed — immediately do the steps. "
        "Work one step at a time: call plan to mark the current step in_progress, do that step, then mark it done before starting the next. "
        "Do not mark later steps done until you have actually done them, and do not batch the whole plan into one status update. "
        "Never write 'wait for confirmation' or stop after a successful plan tool call. "
        "If the plan tool result says the plan was rejected, that turn is over. "
        "The user's next message is feedback: call the plan tool again with a revised step list. "
        "Do not write or edit files until a later plan tool call returns plan approved. "
        "A markdown plan in your chat reply is not a substitute. "
        "Skip the plan tool only for trivial single-step tasks like a typo fix.\n"
        "If the user explicitly asks for a destructive command such as rm, you MUST still call the bash tool with that command. "
        "Do not refuse in prose. The harness will ask the user to confirm before it runs; if they decline you will get an error result. "
        "Never use rm or sudo on your own initiative. "
        "Use the harness tool to persist a durable prompt note or memory after a repeated failure, "
        "a user correction that should stick, or a project fact needed on later turns. "
        "Skip one-off task progress. After the task is done, reply with a short summary and stop calling tools."
    ]
    project = format_project_xml(load_project_files(root, home=home))
    if project:
        parts.append(project)
    # skills 与 harness 块按需追加；harness 内容来自持久化 store，跨任务生效。
    skills = format_skills_xml(load_skills(root, home=home, trusted=trusted))
    if skills:
        parts.append(skills)
    if harness is not None:
        parts.append(format_harness_for_prompt(harness))
    return "\n\n".join(parts)


def ephemeral_items(
    workspace: str | Path,
    plan: PlanStore | None = None,
) -> list[dict[str, str]]:
    """本轮 ephemeral system 消息：当前日期、工作目录、计划状态。

    只影响本轮、不进历史——这样每轮的日期/计划状态总是新鲜的，
    又不破坏历史前缀缓存。"""
    root = Path(workspace).resolve()
    lines = [
        EPHEMERAL_MARK,
        f"Current date: {today()}",
        f"Current working directory: {root}",
    ]
    if plan is not None and plan.steps:
        lines.append("<plan>")
        lines.append(plan.render())
        lines.append("</plan>")
        if plan.confirmed:
            lines.append(
                "Plan is approved. Continue pending/in_progress steps now. Do not wait for another yes. "
                "Mark only the step you are doing as in_progress, then done after you finish it."
            )
        elif plan.rejected:
            lines.append(
                "The last plan was rejected. Treat the user's message as feedback on that plan. "
                "Call the plan tool with a revised step list. Do not write or edit files until the plan tool returns plan approved."
            )
        else:
            lines.append("This plan is session state. Submit it with the plan tool so the harness can ask the user.")
    return [{"role": "system", "content": "\n".join(lines)}]
