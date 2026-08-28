from __future__ import annotations

import json
import os
import re
from typing import Any

from wheel_agent.compact import serialize_items
from wheel_agent.harness import (
    HarnessState,
    HarnessStore,
    apply_proposal,
    generate_refinement_id,
    load_history,
    rollback_proposal,
    snapshot_state,
)
from wheel_agent.model import ModelClient, extract_text
from wheel_agent.types import Item, Usage

REFINEMENT_INSTRUCTIONS = """You are Wheel's /refine continual harness subsystem.

Improve the editable continual harness from the current trajectory.
The base system prompt is immutable. You may only Create, Update, or Delete:
- prompt: narrow behavioral policy addendums (how the agent should act)
- memory: durable facts, decisions, failures, preferences, project knowledge

Local (default): session-specific notes, current-run coordination, project facts that should not leak to other sessions.
Global: stable cross-session lessons, user preferences, or project-qualified facts likely reused later.
During a local refinement, treat global entries as read-only context. Create a local override instead of updating them.

Prefer small evidence-backed edits. If nothing useful is justified, return an empty edits array.
Never edit source files. Output JSON only:

{
  "summary": "one sentence",
  "rationale": "why these edits are justified by trajectory evidence",
  "expectedOutcome": "what should improve and how to validate it",
  "edits": [
    {
      "action": "create|update|delete",
      "kind": "prompt|memory",
      "id": "stable id for update/delete, optional for create",
      "title": "required for create/update",
      "content": "required for create/update",
      "path": "optional grouping path",
      "reason": "why this edit is useful"
    }
  ]
}"""

CONVERSATION_CHARS = 80_000
TRUNCATED_JSON = (
    "the model stopped before completing its JSON object; retry with a smaller request"
)


def parse_auto_refine_every(raw: str | None = None) -> int:
    text = (raw if raw is not None else os.getenv("WHEEL_AUTO_REFINE", "8")).strip().lower()
    if text in {"0", "false", "no", "off"}:
        return 0
    if text in {"", "on", "true", "yes"}:
        return 8
    try:
        return max(0, int(text))
    except ValueError:
        return 8


def refine_due(user_turns: int, every: int, last_at: int) -> bool:
    if every <= 0 or user_turns < every:
        return False
    return user_turns - last_at >= every


def parse_refine_args(args: str) -> dict[str, Any]:
    rest = (args or "").strip()
    global_ = False
    if rest.startswith("--global"):
        global_ = True
        rest = rest[len("--global") :].strip()
    if rest == "rollback":
        raise ValueError("usage: /refine rollback <id>")
    match = re.match(r"^rollback\s+", rest)
    if match:
        rollback_id = rest[match.end() :].strip()
        if rollback_id.endswith(" --global"):
            global_ = True
            rollback_id = rollback_id[: -len(" --global")].strip()
        if rollback_id == "--global" or not rollback_id:
            raise ValueError("usage: /refine rollback <id>")
        return {"rollback_id": rollback_id, "global": global_}
    return {"instructions": rest or None, "global": global_}


def extract_json_object(text: str) -> dict[str, Any]:
    trimmed = (text or "").strip()
    if not trimmed:
        raise ValueError("refiner returned no text")
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", trimmed)
    if fenced:
        trimmed = fenced.group(1).strip()
    if trimmed.startswith("{") and trimmed.endswith("}"):
        return _parse_json(trimmed)
    start = trimmed.find("{")
    end = trimmed.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(trimmed[start : end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            return _parse_json(trimmed[start:])
    return _parse_json(trimmed)


def normalize_proposal(value: Any) -> dict[str, Any]:
    record = value if isinstance(value, dict) else {}
    edits = record.get("edits") if isinstance(record.get("edits"), list) else []
    return {
        "summary": record["summary"] if isinstance(record.get("summary"), str) else "Refined continual harness",
        "rationale": record["rationale"] if isinstance(record.get("rationale"), str) else "",
        "expectedOutcome": record["expectedOutcome"] if isinstance(record.get("expectedOutcome"), str) else "",
        "edits": [edit for edit in edits if isinstance(edit, dict)],
    }


def plan_refinement(
    items: list[Item],
    state: HarnessState,
    history: list[dict[str, Any]],
    model: ModelClient,
    *,
    instructions: str | None = None,
    rollback_id: str | None = None,
    global_: bool = False,
) -> tuple[dict[str, Any], str, str | None, Usage]:
    refinement_id = generate_refinement_id()
    usage = Usage()
    if rollback_id:
        target = next((item for item in history if item.get("id") == rollback_id), None)
        if target is None:
            raise ValueError(f"refinement {rollback_id} not found")
        return rollback_proposal(target), refinement_id, target["id"], usage
    overview = _overview(state)
    history_text = _history_for_prompt(history)
    conversation = serialize_items(items)[-CONVERSATION_CHARS:]
    scope_line = (
        "Requested refinement scope: global. Only propose stable cross-session lessons, "
        "durable user preferences, or explicitly project-qualified facts."
        if global_
        else "Requested refinement scope: local. Prefer session-specific notes. "
        "Global entries are read-only; create a local entry instead of updating them."
    )
    prompt = "\n\n".join(
        part
        for part in (
            f"<current_harness_state>\n{overview}\n</current_harness_state>",
            f"<refinement_history>\n{history_text}\n</refinement_history>",
            f"<conversation>\n{conversation}\n</conversation>",
            f"<scope_policy>\n{scope_line}\n</scope_policy>",
            f"<user_refine_instructions>\n{instructions}\n</user_refine_instructions>" if instructions else "",
            "Return only JSON edits. If no useful edit is justified, return an empty edits array with a rationale.",
        )
        if part
    )
    text, usage = _complete_json(model, prompt)
    return normalize_proposal(extract_json_object(text)), refinement_id, None, usage


def run_refine(
    store: HarnessStore,
    items: list[Item],
    model: ModelClient,
    *,
    instructions: str | None = None,
    rollback_id: str | None = None,
    global_: bool = False,
) -> tuple[dict[str, Any], Usage]:
    local_hist = load_history(store.history_file(False))
    global_hist = load_history(store.history_file(True))
    history = _merge_history(global_hist, local_hist)
    apply_global = global_
    if rollback_id:
        hit = next((item for item in history if item.get("id") == rollback_id), None)
        if hit is None:
            raise ValueError(f"refinement {rollback_id} not found")
        apply_global = hit.get("scope") == "global"
    target = store.target(apply_global)
    merged = store.merged()
    baseline = snapshot_state(target)
    proposal, refinement_id, rollback_of, usage = plan_refinement(
        items,
        merged if not rollback_id else target,
        history,
        model,
        instructions=instructions,
        rollback_id=rollback_id,
        global_=apply_global,
    )
    result = apply_proposal(
        target,
        proposal,
        refinement_id=refinement_id,
        rollback_of=rollback_of,
        scope="global" if apply_global else "local",
        baseline=None if rollback_id else baseline,
    )
    store.record(result)
    return result, usage


def format_refine_result(result: dict[str, Any]) -> str:
    applied = [row for row in result.get("appliedEdits") or [] if row.get("applied")]
    failed = [row for row in result.get("appliedEdits") or [] if not row.get("applied")]
    lines = [f"{result.get('scope', 'local')} {result['id']}: {result.get('summary') or ''}"]
    if result.get("rollbackOf"):
        lines.append(f"rollback of {result['rollbackOf']}")
    if result.get("rationale"):
        lines.append(result["rationale"])
    for row in applied:
        lines.append(f"+ {row.get('action')} {row.get('kind')}:{row.get('id')}")
        title, content = _edit_text(row)
        if title:
            lines.append(title)
        if content:
            lines.append(content)
    for row in failed:
        lines.append(f"! {row.get('action')} {row.get('kind')}:{row.get('id')} {row.get('error')}")
    if not applied and not failed:
        lines.append("  (no edits)")
    return "\n".join(lines)


def _edit_text(row: dict[str, Any]) -> tuple[str, str]:
    after = row.get("after") if isinstance(row.get("after"), dict) else {}
    before = row.get("before") if isinstance(row.get("before"), dict) else {}
    source = after or before
    title = str(row.get("title") or source.get("title") or "").strip()
    content = str(row.get("content") or source.get("content") or "").strip()
    return title, content


def _complete_json(model: ModelClient, prompt: str) -> tuple[str, Usage]:
    old_effort = getattr(model, "effort", None)
    if old_effort is not None:
        model.effort = "off"
    try:
        response = model.complete(
            [{"role": "user", "content": prompt}],
            tools=[],
            instructions=REFINEMENT_INSTRUCTIONS,
        )
    finally:
        if old_effort is not None:
            model.effort = old_effort
    text = extract_text(response.output).strip()
    if not text:
        raise ValueError(TRUNCATED_JSON)
    return text, response.usage


def _parse_json(candidate: str) -> dict[str, Any]:
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        if _incomplete(candidate):
            raise ValueError(TRUNCATED_JSON) from exc
        raise ValueError(f"the model did not return valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("refiner JSON must be an object")
    return value


def _incomplete(candidate: str) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for char in candidate:
        if escaped:
            escaped = False
            continue
        if in_string:
            if char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return in_string or depth > 0


def _overview(state: HarnessState) -> str:
    lines: list[str] = []
    for kind in state.entries:
        entries = list(state.entries[kind].values())
        lines.append(f"{kind}: {len(entries)}")
        for entry in entries[:40]:
            content = re.sub(r"\s+", " ", entry.content)[:240]
            lines.append(f"- [{entry.scope}:{entry.id}] {entry.title} ({entry.path}, v{entry.version}): {content}")
        if len(entries) > 40:
            lines.append(f"- +{len(entries) - 40} more {kind} entries")
    return "\n".join(lines) if lines else "No saved harness entries yet."


def _history_for_prompt(history: list[dict[str, Any]]) -> str:
    if not history:
        return "No prior refinement history."
    blocks = []
    for item in history[-20:]:
        edits = ", ".join(
            f"{'applied' if edit.get('applied') else 'failed'} {edit.get('action')} {edit.get('kind')}:{edit.get('id')}"
            for edit in (item.get("appliedEdits") or [])
        )
        rollback = f" rollbackOf={item['rollbackOf']}" if item.get("rollbackOf") else ""
        blocks.append(
            f"[{item.get('id')}]{rollback} {item.get('summary')}\n{edits}\nExpected outcome: {item.get('expectedOutcome')}"
        )
    return "\n\n".join(blocks)


def _merge_history(global_items: list[dict[str, Any]], local_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in global_items}
    for item in local_items:
        by_id[item["id"]] = item
    return list(by_id.values())
