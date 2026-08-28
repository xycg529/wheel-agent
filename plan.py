from __future__ import annotations

from collections.abc import Callable
from typing import Any

STATUSES = ("pending", "in_progress", "done")


class PlanRejected(Exception):
    """User declined a plan confirmation."""


REJECTED_HINT = (
    "plan rejected by user — do not implement. "
    "On the next user message, call the plan tool again with a revised step list "
    "that incorporates their feedback. The harness will ask y/N again."
)


class PlanStore:
    def __init__(self, *, ask: Callable[[str], bool] | None = None, interactive: bool = False):
        self.steps: list[dict[str, str]] = []
        self.confirmed = False
        self.rejected = False
        self.ask = ask
        self.interactive = interactive

    def replace(self, raw: list[Any]) -> str:
        steps = normalize_steps(raw)
        new_contents = [step["content"] for step in steps]
        old_contents = [step["content"] for step in self.steps]
        old_done = {step["content"] for step in self.steps if step["status"] == "done"}
        needs_confirm = (not self.confirmed) or new_contents != old_contents
        if needs_confirm:
            if self.interactive and self.ask:
                prompt = "Proposed plan:\n" + format_plan(steps) + "\nProceed with this plan?"
                if not self.ask(prompt):
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
        extra = ""
        if jumped > 1:
            extra = "\nnote: mark one newly finished step per plan call so progress stays visible."
        return "plan updated\n" + format_plan(steps) + extra

    def render(self) -> str:
        return format_plan(self.steps) if self.steps else "(empty plan)"

    def footer_lines(self, *, busy: bool = False, max_lines: int = 10) -> list[str]:
        if not self.steps:
            return []
        if (not busy) and (not self.rejected) and all(step["status"] == "done" for step in self.steps):
            return []
        lines = format_plan(self.steps).splitlines()
        if len(lines) > max_lines:
            return lines[: max_lines - 1] + ["…"]
        return lines


def normalize_steps(raw: list[Any]) -> list[dict[str, str]]:
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
    marks = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}
    lines = []
    for i, step in enumerate(steps, start=1):
        lines.append(f"{i}. {marks.get(step['status'], '[ ]')} {step['content']}")
    return "\n".join(lines)
