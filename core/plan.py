"""计划状态与拒绝机制：plan 工具的步骤校验、用户确认流、页脚渲染。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# 计划步骤的三种状态；completed 是常见别名，读入时归一成 done。
STATUSES = ("pending", "in_progress", "done")


class PlanRejected(Exception):
    """用户拒绝了计划确认。循环把它转成 plan_rejected 停止原因。"""


REJECTED_HINT = (
    # 拒绝后回给模型的提示：引导它改计划，而不是直接开写。
    "plan rejected by user — do not implement. "
    "On the next user message, call the plan tool again with a revised step list "
    "that incorporates their feedback. The harness will ask y/N again."
)


class PlanStore:
    """当前计划的状态机：步骤列表 + 三位状态标记（已批准/已拒绝/待确认）。"""

    def __init__(self, *, ask: Callable[[str], bool] | None = None, interactive: bool = False):
        self.steps: list[dict[str, str]] = []
        # confirmed：已获用户批准；rejected：上次提交被拒，写/编辑前必须先改计划。
        self.confirmed = False
        self.rejected = False
        self.ask = ask
        self.interactive = interactive

    def replace(self, raw: list[Any]) -> str:
        """整体替换当前计划（模型每次都发全部步骤），返回回给模型的文本。

        首次提交或内容有变化都要用户 y/N 确认；拒绝时置 rejected 并抛
        PlanRejected（循环随即停机）。内容没变的进度更新免确认，但检测到
        一次标完多个 done 时附提示。"""
        steps = normalize_steps(raw)
        new_contents = [step["content"] for step in steps]
        old_contents = [step["content"] for step in self.steps]
        old_done = {step["content"] for step in self.steps if step["status"] == "done"}
        # 已批准且步骤内容没变 → 进度更新，免二次确认。
        needs_confirm = (not self.confirmed) or new_contents != old_contents
        if needs_confirm:
            if self.interactive and self.ask:
                prompt = "Proposed plan:\n" + format_plan(steps) + "\nProceed with this plan?"
                if not self.ask(prompt):
                    # 只在从未批准过时记 rejected：已批准的计划里改步骤被拒
                    # 不该卡死整个任务。
                    if not self.confirmed:
                        self.steps = steps
                        self.rejected = True
                    raise PlanRejected(REJECTED_HINT)
            self.confirmed = True
            self.rejected = False
            self.steps = steps
            return (
                "plan approved — continue with these steps now. "
                "Do not ask for another confirmation in chat.\n"
                + format_plan(steps)
            )
        self.steps = steps
        jumped = len({step["content"] for step in steps if step["status"] == "done"} - old_done)
        # 一次标完多个 done：进度不可见，附提示引导逐步标记。
        extra = ""
        if jumped > 1:
            extra = "\nnote: mark one newly finished step per plan call so progress stays visible."
        return "plan updated\n" + format_plan(steps) + extra

    def render(self) -> str:
        """完整计划文本（/plan 命令、ephemeral 上下文用）。"""
        return format_plan(self.steps) if self.steps else "(empty plan)"

    def footer_lines(self, *, busy: bool = False, max_lines: int = 10) -> list[str]:
        """页脚展示的计划行：全部完成后且空闲时隐藏，避免占屏。"""
        if not self.steps:
            return []
        if (not busy) and (not self.rejected) and all(step["status"] == "done" for step in self.steps):
            return []
        lines = format_plan(self.steps).splitlines()
        if len(lines) > max_lines:
            return lines[: max_lines - 1] + ["…"]
        return lines


def normalize_steps(raw: list[Any]) -> list[dict[str, str]]:
    """校验模型发来的步骤：非空、去重、状态合法、最多一个 in_progress。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("plan requires a non-empty steps array")
    steps: list[dict[str, str]] = []
    seen: set[str] = set()
    active = 0
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each step must be an object with content and status")
        content = str(item.get("content") or "").strip()
        status = str(item.get("status") or "pending").strip().lower()
        if status == "completed":
            status = "done"
        if not content:
            raise ValueError("step content must be non-empty")
        if status not in STATUSES:
            raise ValueError(f"status must be pending|in_progress|done, got {status!r}")
        if content in seen:
            raise ValueError(f"duplicate step: {content}")
        seen.add(content)
        if status == "in_progress":
            active += 1
        steps.append({"content": content, "status": status})
    if active > 1:
        raise ValueError(f"at most one step may be in_progress (got {active})")
    return steps


def format_plan(steps: list[dict[str, str]]) -> str:
    """渲染成 [ ]/[>]/[x] 复选框样式的编号清单。"""
    marks = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}
    lines = []
    for i, step in enumerate(steps, start=1):
        lines.append(f"{i}. {marks.get(step['status'], '[ ]')} {step['content']}")
    return "\n".join(lines)
