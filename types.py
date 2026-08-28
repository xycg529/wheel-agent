from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Item = dict[str, Any]
Decision = Literal["allow", "ask", "deny"]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_tokens += other.cached_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.reasoning_tokens += other.reasoning_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Usage":
        data = data or {}
        return cls(
            input_tokens=int(data.get("input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
            cached_tokens=int(data.get("cached_tokens") or 0),
            cache_write_tokens=int(data.get("cache_write_tokens") or 0),
            reasoning_tokens=int(data.get("reasoning_tokens") or 0),
        )


class APIError(RuntimeError):
    """Provider call failed after retries. Session should keep already-finished turns."""

    def __init__(self, message: str, *, transient: bool = False, status: int | None = None):
        super().__init__(message)
        self.transient = transient
        self.status = status


@dataclass
class ModelResponse:
    output: list[Item]
    usage: Usage = field(default_factory=Usage)
    raw_id: str = ""


@dataclass
class FunctionCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass
class ToolResult:
    call_id: str
    name: str
    output: str
    is_error: bool = False
    blocked: bool = False
    safety_decision: str = ""
    safety_reason: str = ""
    safety_source: str = ""


@dataclass
class SafetyVerdict:
    decision: Decision
    reason: str
    source: str = "rules"


@dataclass
class RunResult:
    run_id: str
    text: str
    turns: int
    usage: Usage
    tool_results: list[ToolResult]
    stop_reason: str
    events_path: str = ""
    items: list[Item] = field(default_factory=list)
    last_usage: Usage = field(default_factory=Usage)
    changed_files: list[str] = field(default_factory=list)
    task_id: str = ""
    replay_status: str = ""
    replay_details: dict[str, Any] = field(default_factory=dict)
