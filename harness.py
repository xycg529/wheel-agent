from __future__ import annotations

import json
import itertools
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from secrets import token_hex
from typing import Any

KINDS = ("prompt", "memory")
SCOPES = ("local", "global")
GLOBAL_DIRNAME = "harness"
STATE_NAME = "harness_state.json"
HISTORY_NAME = "refinements.jsonl"
MAX_ENTRIES_PER_KIND = 8
MAX_CONTENT = 240


def now() -> str:
    return datetime.now().astimezone().isoformat()


def slug(raw: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")
    return (normalized or fallback)[:80]


_id_counter = itertools.count(1)
_id_lock = threading.Lock()
_PROC_NONCE = token_hex(2)


def generate_refinement_id() -> str:
    # Two harness tool calls in one batch land in the same millisecond, so the
    # timestamp alone is not unique: nonce (process) + counter (in-process)
    # make collisions impossible where the history file is shared.
    with _id_lock:
        n = next(_id_counter)
    stamp = datetime.now().astimezone().strftime("%Y%m%d%H%M%S%f")[:17]
    return f"refine_{stamp}_{_PROC_NONCE}{n:04d}"


@dataclass
class HarnessEntry:
    id: str
    kind: str
    title: str
    content: str
    path: str = "general"
    scope: str = "local"
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str = "agent"
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    version: int = 1


@dataclass
class HarnessState:
    entries: dict[str, dict[str, HarnessEntry]] = field(
        default_factory=lambda: {kind: {} for kind in KINDS}
    )
    refinements: list[dict[str, Any]] = field(default_factory=list)
    path: Path | None = None
    scope: str = "local"
    schema: int = 1

    def clone_entry(self, kind: str, entry_id: str) -> HarnessEntry | None:
        entry = self.entries.get(kind, {}).get(entry_id)
        if entry is None:
            return None
        return HarnessEntry(**asdict(entry))


def empty_state(path: Path | None = None, scope: str = "local") -> HarnessState:
    return HarnessState(path=path, scope=scope)


def global_harness_dir(home: str | Path | None = None) -> Path:
    root = Path(home).expanduser() if home is not None else Path.home()
    return root / ".wheel" / GLOBAL_DIRNAME


def local_harness_path(
    workspace: str | Path,
    session_path: str | Path | None = None,
) -> Path:
    if session_path is not None:
        return Path(session_path).with_suffix(".harness.json")
    return Path(workspace).resolve() / ".wheel" / STATE_NAME


def history_path(state_path: Path) -> Path:
    if state_path.name.endswith(".harness.json"):
        return state_path.with_suffix("").with_suffix(".refinements.jsonl")
    return state_path.with_name(HISTORY_NAME)


def load_state(path: Path, scope: str = "local") -> HarnessState:
    state = empty_state(path, scope)
    if not path.exists():
        return state
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if not isinstance(raw, dict):
        return state
    state.schema = 1
    try:
        state.schema = int(raw.get("schema") or 1)
    except (TypeError, ValueError):
        pass  # corrupt schema value: degrade to v1 like other corrupt fields
    records = raw.get("entries") if isinstance(raw.get("entries"), dict) else {}
    for kind in KINDS:
        bucket = records.get(kind) if isinstance(records.get(kind), dict) else {}
        for entry_id, item in bucket.items():
            entry = _parse_entry(entry_id, kind, item, scope)
            if entry is not None:
                state.entries[kind][entry.id] = entry
    events = raw.get("refinements")
    if isinstance(events, list):
        state.refinements = [event for event in events if isinstance(event, dict)]
    return state


def save_state(state: HarnessState) -> Path | None:
    if state.path is None:
        return None
    path = state.path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": state.schema,
        "entries": {
            kind: {eid: asdict(entry) for eid, entry in records.items()}
            for kind, records in state.entries.items()
        },
        "refinements": state.refinements,
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return path


def snapshot_state(state: HarnessState) -> HarnessState:
    return HarnessState(
        entries={
            kind: {eid: HarnessEntry(**asdict(entry)) for eid, entry in bucket.items()}
            for kind, bucket in state.entries.items()
        },
        refinements=list(state.refinements),
        path=state.path,
        scope=state.scope,
        schema=state.schema,
    )


def merge_states(global_state: HarnessState, local_state: HarnessState | None) -> HarnessState:
    merged = empty_state(scope="local")
    merged.schema = max(global_state.schema, local_state.schema if local_state else 1)
    for kind in KINDS:
        for eid, entry in global_state.entries[kind].items():
            cloned = HarnessEntry(**asdict(entry))
            cloned.scope = "global"
            merged.entries[kind][eid] = cloned
        if local_state is None:
            continue
        for eid, entry in local_state.entries[kind].items():
            cloned = HarnessEntry(**asdict(entry))
            cloned.scope = "local"
            key = f"local:{eid}" if eid in merged.entries[kind] else eid
            merged.entries[kind][key] = cloned
    merged.refinements = list(global_state.refinements)
    if local_state is not None:
        merged.refinements.extend(local_state.refinements)
    return merged


def format_harness_for_prompt(
    state: HarnessState,
    *,
    max_entries: int = MAX_ENTRIES_PER_KIND,
    max_content: int | None = MAX_CONTENT,
) -> str:
    total = sum(len(bucket) for bucket in state.entries.values())
    lines = [
        "# Continual harness",
        "Prompt notes and memories persist outside the chat. Follow them. The base system prompt is immutable.",
        "",
    ]
    if total == 0:
        lines.append("No saved entries. Use the harness tool for durable lessons; skip one-off task state.")
        return "\n".join(lines)
    for kind in KINDS:
        entries = sorted(
            state.entries[kind].values(),
            key=lambda item: (item.path, item.title, item.id),
        )
        lines.append(f"{kind}: {len(entries)}")
        for entry in entries[:max_entries]:
            body = entry.content if max_content is None else _compact(entry.content, max_content)
            lines.append(
                f"- [{entry.scope}:{entry.id}] {entry.title} ({entry.path}, v{entry.version}): {body}"
            )
        overflow = len(entries) - min(len(entries), max_entries)
        if overflow:
            lines.append(f"- +{overflow} more {kind} entries")
        lines.append("")
    if state.refinements:
        lines.append(f"recent refinements: {len(state.refinements)}")
        for event in state.refinements[-5:]:
            trigger = str(event.get("trigger") or "")
            if max_content is not None:
                trigger = _compact(trigger, max_content)
            changes = ", ".join(str(item) for item in (event.get("changes") or [])[:6]) or "no applied edits"
            lines.append(f"- [{event.get('id')}] {trigger}: {changes}")
    return "\n".join(lines).strip()


def apply_proposal(
    state: HarnessState,
    proposal: dict[str, Any],
    *,
    refinement_id: str,
    rollback_of: str | None = None,
    scope: str | None = None,
    baseline: HarnessState | None = None,
) -> dict[str, Any]:
    applied: list[dict[str, Any]] = []
    target_scope = scope or state.scope
    seen: set[str] = set()
    for raw in proposal.get("edits") or []:
        if not isinstance(raw, dict):
            continue
        edit = dict(raw)
        computed_id = edit.get("id") or (
            slug(str(edit.get("title") or edit.get("kind") or "entry"), str(edit.get("kind") or "entry"))
            if edit.get("action") == "create"
            else ""
        )
        edit_id = str(computed_id or "")
        error = _validate_edit(edit, edit_id)
        if error:
            applied.append({**edit, "id": edit_id, "applied": False, "error": error})
            continue
        kind = str(edit["kind"])
        before = state.clone_entry(kind, edit_id)
        key = f"{kind}:{edit_id}"
        if baseline is not None and key not in seen:
            expected = baseline.clone_entry(kind, edit_id)
            current = asdict(before) if before else None
            wanted = asdict(expected) if expected else None
            if json.dumps(current, sort_keys=True) != json.dumps(wanted, sort_keys=True):
                applied.append(
                    {
                        **edit,
                        "id": edit_id,
                        "before": current,
                        "applied": False,
                        "error": "entry changed during refinement planning",
                    }
                )
                continue
        action = edit["action"]
        if action == "delete":
            if before is None:
                applied.append({**edit, "id": edit_id, "applied": False, "error": "entry not found"})
                continue
            del state.entries[kind][edit_id]
            seen.add(key)
            applied.append({**edit, "id": edit_id, "before": asdict(before), "applied": True})
            continue
        if action == "create" and before is not None:
            applied.append({**edit, "id": edit_id, "before": asdict(before), "applied": False, "error": "entry already exists"})
            continue
        if action == "update" and before is None:
            applied.append({**edit, "id": edit_id, "applied": False, "error": "entry not found"})
            continue
        after = HarnessEntry(
            id=edit_id,
            kind=kind,
            title=str(edit.get("title") or (before.title if before else edit_id)),
            content=str(edit.get("content") or (before.content if before else "")),
            path=str(edit.get("path") or (before.path if before else "general")),
            scope=before.scope if before else target_scope,
            metadata=dict(edit.get("metadata") or (before.metadata if before else {})),
            source="refine",
            created_at=before.created_at if before else now(),
            updated_at=now(),
            version=(before.version + 1) if before else 1,
        )
        state.entries[kind][edit_id] = after
        seen.add(key)
        applied.append(
            {
                **edit,
                "id": edit_id,
                "before": asdict(before) if before else None,
                "after": asdict(after),
                "applied": True,
            }
        )
    changes = [f"{row['action']} {row['kind']}:{row['id']}" for row in applied if row.get("applied")]
    state.refinements.append(
        {
            "id": refinement_id,
            "trigger": str(proposal.get("summary") or ""),
            "changes": changes,
            "evidence": str(proposal.get("rationale") or ""),
            "outcome": str(proposal.get("expectedOutcome") or ""),
            "created_at": now(),
        }
    )
    save_state(state)
    return {
        "id": refinement_id,
        "summary": str(proposal.get("summary") or "Refined continual harness"),
        "rationale": str(proposal.get("rationale") or ""),
        "expectedOutcome": str(proposal.get("expectedOutcome") or ""),
        "appliedEdits": applied,
        "harnessStatePath": str(state.path or ""),
        "rollbackOf": rollback_of,
        "scope": target_scope,
    }


def rollback_proposal(target: dict[str, Any]) -> dict[str, Any]:
    edits: list[dict[str, Any]] = []
    for edit in reversed(target.get("appliedEdits") or []):
        if not edit.get("applied"):
            continue
        before = edit.get("before")
        after = edit.get("after")
        if before:
            edits.append(
                {
                    "action": "update" if after else "create",
                    "kind": edit["kind"],
                    "id": edit["id"],
                    "title": before["title"],
                    "content": before["content"],
                    "path": before.get("path") or "general",
                    "metadata": before.get("metadata") or {},
                    "reason": f"Rollback {target.get('id')}",
                }
            )
        elif after:
            edits.append(
                {
                    "action": "delete",
                    "kind": edit["kind"],
                    "id": edit["id"],
                    "reason": f"Rollback {target.get('id')}",
                }
            )
    return {
        "summary": f"Rollback refinement {target.get('id')}",
        "rationale": f"Restores harness snapshots from refinement {target.get('id')}.",
        "expectedOutcome": "Faulty refinement edits are reverted.",
        "edits": edits,
    }


def append_history(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def load_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("id") and "appliedEdits" in item:
            out.append(item)
    return out


class HarnessStore:
    def __init__(
        self,
        local: HarnessState,
        global_state: HarnessState,
        *,
        interactive: bool = True,
    ):
        self.local = local
        self.global_state = global_state
        self.interactive = interactive
        self.dirty = False

    @classmethod
    def for_workspace(
        cls,
        workspace: str | Path,
        *,
        session_path: str | Path | None = None,
        home: str | Path | None = None,
        interactive: bool = True,
    ) -> "HarnessStore":
        local_path = local_harness_path(workspace, session_path)
        global_path = global_harness_dir(home) / STATE_NAME
        return cls(
            load_state(local_path, "local"),
            load_state(global_path, "global"),
            interactive=interactive,
        )

    def merged(self) -> HarnessState:
        return merge_states(self.global_state, self.local)

    def target(self, global_: bool) -> HarnessState:
        if global_ and not self.interactive:
            raise ValueError("global harness writes are interactive-only")
        return self.global_state if global_ else self.local

    def history_file(self, global_: bool) -> Path:
        state = self.global_state if global_ else self.local
        if state.path is None:  # public API allows path-less states; fail clearly, not with an assert
            raise ValueError("harness state has no file path; cannot record history")
        return history_path(state.path)

    def record(self, result: dict[str, Any]) -> None:
        global_ = result.get("scope") == "global"
        path = self.history_file(global_)
        append_history(path, result)
        self.dirty = True

    def dispatch(self, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "list").strip().lower()
        global_ = _as_bool(args.get("global"), False)
        if action == "list":
            text = format_harness_for_prompt(self.merged())
            return text or "No harness entries."
        kind = str(args.get("kind") or "").strip().lower()
        entry_id = str(args.get("id") or "").strip()
        title = str(args.get("title") or "").strip()
        content = str(args.get("content") or "")
        path = str(args.get("path") or "general").strip() or "general"
        if action == "create":
            proposal = {
                "summary": f"create {kind}",
                "rationale": "harness tool",
                "expectedOutcome": "entry available on later turns",
                "edits": [
                    {
                        "action": "create",
                        "kind": kind,
                        "id": entry_id or None,
                        "title": title,
                        "content": content,
                        "path": path,
                    }
                ],
            }
        elif action == "update":
            proposal = {
                "summary": f"update {kind}:{entry_id}",
                "rationale": "harness tool",
                "expectedOutcome": "entry updated",
                "edits": [
                    {
                        "action": "update",
                        "kind": kind,
                        "id": entry_id,
                        "title": title,
                        "content": content,
                        "path": path,
                    }
                ],
            }
        elif action == "delete":
            proposal = {
                "summary": f"delete {kind}:{entry_id}",
                "rationale": "harness tool",
                "expectedOutcome": "entry removed",
                "edits": [{"action": "delete", "kind": kind, "id": entry_id}],
            }
        else:
            raise ValueError(f"unsupported harness action {action!r}")
        result = apply_proposal(
            self.target(global_),
            proposal,
            refinement_id=generate_refinement_id(),
            scope="global" if global_ else "local",
        )
        applied = [row for row in result["appliedEdits"] if row.get("applied")]
        failed = [row for row in result["appliedEdits"] if not row.get("applied")]
        if not applied:
            error = failed[0]["error"] if failed else "no edits applied"
            raise ValueError(error)
        self.record(result)
        return _format_tool_result(result)


def _parse_entry(entry_id: str, kind: str, raw: Any, scope: str) -> HarnessEntry | None:
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    content = raw.get("content")
    if not isinstance(title, str) or not isinstance(content, str):
        return None
    meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    version = raw.get("version", 1)
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = 1
    entry_scope = raw.get("scope") if raw.get("scope") in SCOPES else scope
    return HarnessEntry(
        id=str(raw.get("id") or entry_id),
        kind=kind,
        title=title,
        content=content,
        path=str(raw.get("path") or "general"),
        scope=entry_scope,
        metadata=dict(meta),
        source=str(raw.get("source") or "agent"),
        created_at=str(raw.get("created_at") or now()),
        updated_at=str(raw.get("updated_at") or now()),
        version=version,
    )


def _validate_edit(edit: dict[str, Any], computed_id: str) -> str | None:
    action = edit.get("action")
    kind = edit.get("kind")
    if action not in {"create", "update", "delete"}:
        return f"unsupported action {action!r}"
    if kind not in KINDS:
        return f"unsupported kind {kind!r}"
    if kind == "prompt" and (edit.get("id") == "base_system_prompt" or computed_id == "base_system_prompt"):
        return "base system prompt is not editable"
    if action != "create" and not edit.get("id"):
        return f"{action} requires id"
    if action != "delete" and (
        not str(edit.get("title") or "").strip() or not str(edit.get("content") or "").strip()
    ):
        return f"{action} requires title and content"
    return None


def _compact(text: str, max_length: int) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max(0, max_length - 3)] + "..."


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _format_tool_result(result: dict[str, Any]) -> str:
    lines = [f"harness {result.get('scope')} {result['id']}: {result['summary']}"]
    for edit in result.get("appliedEdits") or []:
        mark = "+" if edit.get("applied") else "!"
        detail = edit.get("error") or edit.get("title") or ""
        lines.append(f"  {mark} {edit.get('action')} {edit.get('kind')}:{edit.get('id')} {detail}".rstrip())
    return "\n".join(lines)
