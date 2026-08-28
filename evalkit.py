from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from wheel_agent.config import AgentConfig
from wheel_agent.types import RunResult


def safe_int_env(name: str, default: int) -> int:
    """int(os.environ[name]) that degrades to `default` on garbage instead of crashing an eval run."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def eval_agent_config(base: AgentConfig, runs_dir: Path, default_max_turns: int) -> AgentConfig:
    """Non-interactive config shared by the eval suites.

    REPL/--json runs are unlimited (max_turns=0); eval suites cap at the
    MAX_TURNS env value or the suite default (20 polyglot / 50 SWE).
    """
    return AgentConfig(
        provider=base.provider,
        providers=base.providers,
        max_turns=base.max_turns if base.max_turns > 0 else safe_int_env("MAX_TURNS", default_max_turns),
        runs_dir=runs_dir,
        interactive=False,
        effort=base.effort,
    )


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class TaskOutcome:
    task_id: str
    resolved: bool
    checks: list[CheckResult]
    run: RunResult | None
    status: str = "complete"
    replay_match: bool | None = None
    replay_status: str | None = None


@dataclass
class EvalReport:
    suite: str
    outcomes: list[TaskOutcome]
    status: str = "complete"
    error: str = ""

    def metrics(self) -> dict[str, float | None]:
        available = [item for item in self.outcomes if item.status == "complete"]
        resolved = sum(1 for item in available if item.resolved)
        tool_calls = 0
        tool_ok = 0
        replay_cases = 0
        replay_exact = 0
        for item in available:
            if item.run:
                tool_calls += len(item.run.tool_results)
                tool_ok += sum(1 for t in item.run.tool_results if not t.is_error or t.blocked)
            if item.replay_status:
                replay_cases += 1
                replay_exact += int(item.replay_status == "exact")
            elif item.replay_match is not None:
                replay_cases += 1
                replay_exact += int(item.replay_match)
        return {
            "resolve_rate": (resolved / len(available)) if available else None,
            "tool_success_rate": (tool_ok / tool_calls) if tool_calls else None,
            "replay_exact_rate": (replay_exact / replay_cases) if replay_cases else None,
            "availability_rate": (len(available) / len(self.outcomes)) if self.outcomes else None,
        }

    def format(self) -> str:
        metrics = self.metrics()
        lines = [f"suite={self.suite} tasks={len(self.outcomes)} status={self.status}"]
        if self.error:
            lines.append(f"error: {self.error}")
        for key, value in metrics.items():
            lines.append(f"{key}: {value:.1%}" if value is not None else f"{key}: N/A")
        lines.append("")
        for item in self.outcomes:
            mark = "PASS" if item.resolved else "FAIL"
            replay = ""
            if item.replay_status:
                replay = " replay=" + item.replay_status
            elif item.replay_match is not None:
                replay = " replay=" + ("exact" if item.replay_match else "drift")
            lines.append(f"[{mark}] {item.task_id}{replay}")
            for check in item.checks:
                flag = "  +" if check.passed else "  -"
                lines.append(f"{flag} {check.name}: {check.detail}")
        return "\n".join(lines) + "\n"
