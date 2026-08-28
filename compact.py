from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wheel_agent.context import estimate_item_tokens, estimate_items_tokens, estimate_tokens, tag_lines
from wheel_agent.model import ModelClient, extract_text, item_text
from wheel_agent.types import Item, Usage

RESERVE_TOKENS = 16_384
KEEP_RECENT_TOKENS = 20_000
SUMMARY_MARK = "[SESSION SUMMARY — not a user message]"


@dataclass
class CompactStats:
    did: bool = False
    before_items: int = 0
    after_items: int = 0
    before_tokens: int = 0
    after_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "did": self.did,
            "before_items": self.before_items,
            "after_items": self.after_items,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
        }

SUMMARIZE_INSTRUCTIONS = (
    "You compress a coding-agent conversation into a structured summary. "
    "Return only the summary text, no preamble."
)

SUMMARIZE_PROMPT = """Summarize the following coding-agent history for a future turn.
Preserve decisions, constraints, file paths, and unfinished work.
Use exactly these headings:

## Goal
## Constraints & Preferences
## Progress
### Done
### In Progress
### Blocked
## Key Decisions
## Next Steps
## Critical Context
"""


def should_compact(input_tokens: int, context_window: int, reserve: int = RESERVE_TOKENS) -> bool:
    if context_window <= 0:
        return False
    return input_tokens > context_window - reserve


def is_context_overflow(exc: BaseException) -> bool:
    text = str(exc).lower()
    needles = (
        "context_length_exceeded",
        "context length",
        "maximum context",
        "prompt is too long",
        "too many tokens",
        "token limit",
        "context window",
    )
    return any(needle in text for needle in needles)


def is_summary_item(item: Item) -> bool:
    if item.get("role") != "user":
        return False
    return SUMMARY_MARK in _item_text(item)


def _starts_valid_suffix(item: Item) -> bool:
    """Item that may open the kept suffix right after the injected summary user message."""
    return item.get("role") in {"user", "assistant"} or item.get("type") == "function_call"


def find_user_cut_index(items: list[Item], keep_recent_tokens: int = KEEP_RECENT_TOKENS) -> int | None:
    # Walk backward from the tail until we have budgeted keep_recent_tokens of
    # *kept* history, then snap the cut forward to the next user boundary.
    # Cutting only at user turns keeps the retained suffix a valid sequence
    # (it can never start mid tool_call/tool_output pair, which the API
    # rejects) and gives the summary a clean "everything before this user
    # turn" contract.
    user_indices = [i for i, item in enumerate(items) if item.get("role") == "user"]
    if not user_indices:
        return None
    total = estimate_items_tokens(items)
    if total <= keep_recent_tokens:
        return None
    accumulated = 0
    threshold = 0
    reached = False
    for i in range(len(items) - 1, -1, -1):
        accumulated += estimate_item_tokens(items[i])
        if accumulated >= keep_recent_tokens:
            threshold = i
            reached = True
            break
    if not reached:
        return None
    for idx in user_indices:
        if idx >= threshold and idx > 0:
            return idx
    cut = user_indices[-1]
    if cut > 0:
        return cut
    # Long single-user-turn task: the only user message is the task itself and
    # sits before the threshold, so no user boundary exists. Fall back to the
    # first API-valid boundary (user/assistant message or tool call) so the
    # run can still be compacted instead of growing until overflow.
    for i in range(max(1, threshold), len(items)):
        if _starts_valid_suffix(items[i]):
            return i
    return None


def previous_summary(items: list[Item]) -> str:
    for item in items:
        if is_summary_item(item):
            return _item_text(item)
    return ""


def collect_file_ops(items: list[Item]) -> tuple[list[str], list[str]]:
    read: list[str] = []
    modified: list[str] = []
    prior = previous_summary(items)
    if prior:
        read.extend(tag_lines(prior, "read-files"))
        modified.extend(tag_lines(prior, "modified-files"))
    for item in items:
        if item.get("type") != "function_call":
            continue
        args = item.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            continue
        path = args.get("path")
        if not path:
            continue
        name = str(item.get("name") or "")
        if name == "read":
            read.append(str(path))
        elif name in {"write", "edit"}:
            modified.append(str(path))
    return _unique(read), _unique(modified)


def compact_items(
    items: list[Item],
    model: ModelClient,
    workspace: str | Path,
    *,
    keep_recent_tokens: int = KEEP_RECENT_TOKENS,
    context_window: int = 0,
) -> tuple[list[Item], Usage]:
    usage = Usage()
    cut = find_user_cut_index(items, keep_recent_tokens)
    if cut is None:
        return items, usage
    if context_window > 0:
        kept_est = estimate_items_tokens(items[cut:])
        shrinking = keep_recent_tokens
        while kept_est > context_window - RESERVE_TOKENS and shrinking > 2_000:
            shrinking = shrinking // 2
            nxt = find_user_cut_index(items, shrinking)
            if nxt is None or nxt <= cut:
                break
            cut = nxt
            kept_est = estimate_items_tokens(items[cut:])
    prefix, kept = items[:cut], items[cut:]
    if not prefix:
        return items, usage
    summary, usage = _summarize(prefix, model, items)
    summary_item: Item = {"role": "user", "content": f"{SUMMARY_MARK}\n\n{summary}"}
    return [summary_item, *kept], usage


def compact_history(
    items: list[Item],
    model: ModelClient,
    workspace: str | Path,
    *,
    input_tokens: int = 0,
    context_window: int = 128_000,
    force: bool = False,
    plan_text: str = "",
) -> tuple[list[Item], Usage, CompactStats]:
    usage = Usage()
    # Do not rewrite already-sent items (breaks prompt-cache prefixes).
    # A full compact replaces the prefix with a new summary and starts a new cache epoch.
    # plan_text is a compatibility no-op: plan state now lives in Session.plan,
    # not in the compacted history.
    del plan_text
    before_tokens = estimate_items_tokens(items)
    stats = CompactStats(
        before_items=len(items),
        after_items=len(items),
        before_tokens=before_tokens,
        after_tokens=before_tokens,
    )
    compacted = items
    if force or should_compact(input_tokens, context_window):
        compacted, extra = compact_items(compacted, model, workspace, context_window=context_window)
        usage.add(extra)
        # compact_items returns the *same list object* when it finds nothing
        # to cut, so identity (not length) is the no-op signal.
        stats.did = compacted is not items
        stats.after_items = len(compacted)
        stats.after_tokens = estimate_items_tokens(compacted) if stats.did else before_tokens
    return compacted, usage, stats


def serialize_items(items: list[Item]) -> str:
    lines: list[str] = []
    for item in items:
        kind = item.get("type") or item.get("role") or "item"
        if item.get("role") == "user":
            lines.append(f"[User]: {_item_text(item)}")
        elif kind == "function_call":
            lines.append(f"[Assistant tool call]: {item.get('name')} {item.get('arguments')}")
        elif kind == "function_call_output":
            output = str(item.get("output") or "")
            if len(output) > 4000:
                output = output[:4000] + "…"
            lines.append(f"[Tool result]: {output}")
        elif kind in {"message", "assistant"} or item.get("role") == "assistant":
            text = _item_text(item)
            if text:
                lines.append(f"[Assistant]: {text}")
        else:
            lines.append(f"[{kind}]: {json.dumps(item, ensure_ascii=False)[:2000]}")
    return "\n".join(lines)


def _summarize(prefix: list[Item], model: ModelClient, all_items: list[Item]) -> tuple[str, Usage]:
    prior = previous_summary(prefix)
    read_files, modified_files = collect_file_ops(all_items)
    prompt = SUMMARIZE_PROMPT
    if prior:
        prompt += "\nPrevious summary to update:\n" + prior + "\n"
    prompt += "\nHistory:\n" + serialize_items(prefix)
    response = model.complete(
        [{"role": "user", "content": prompt}],
        tools=[],
        instructions=SUMMARIZE_INSTRUCTIONS,
    )
    text = extract_text(response.output).strip() or serialize_items(prefix)[:2000]
    text = _ensure_file_tags(text, read_files, modified_files)
    return text, response.usage


def _ensure_file_tags(summary: str, read_files: list[str], modified_files: list[str]) -> str:
    body = summary.rstrip()
    if read_files and "<read-files>" not in body:
        body += "\n\n<read-files>\n" + "\n".join(read_files) + "\n</read-files>"
    if modified_files and "<modified-files>" not in body:
        body += "\n\n<modified-files>\n" + "\n".join(modified_files) + "\n</modified-files>"
    return body


def _item_text(item: Item) -> str:
    """model.item_text plus the function_call_output fallback those items carry in 'output'."""
    text = item_text(item)
    if not text and item.get("output"):
        return str(item["output"])
    return text


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
