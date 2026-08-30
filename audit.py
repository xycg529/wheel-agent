from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

from wheel_agent.safety import is_sensitive_path

SKIP_PARTS = {".wheel", ".wheel_runs", ".git", "__pycache__"}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _normalize_for_audit(value: Any, workspace: str | Path | None) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_for_audit(item, workspace) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_for_audit(item, workspace) for item in value]
    if isinstance(value, str) and workspace:
        # Replace both the resolved root and the raw workspace string: on macOS
        # /tmp is a symlink to /private/tmp, so an unresolved workspace (or an
        # unresolved path in tool output) would otherwise survive into the hash.
        root = str(Path(workspace).resolve())
        out = value.replace(root, "<workspace>")
        raw = str(workspace)
        if raw != root:
            out = out.replace(raw, "<workspace>")
        return out
    return value


def item_audit(items: list[dict[str, Any]], workspace: str | Path | None = None) -> dict[str, Any]:
    normalized = _normalize_for_audit(items, workspace)
    return {
        "sha256": sha256_value(normalized),
        "count": len(items),
        "types": [str(item.get("type") or item.get("role") or "") for item in items],
    }


def environment_snapshot() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "executable": os.path.basename(sys.executable),
        "shell": os.path.basename(os.environ.get("SHELL") or ""),
    }


def environment_fingerprint() -> str:
    return sha256_value(environment_snapshot())


def workspace_manifest(root: str | Path) -> dict[str, str]:
    root = Path(root).resolve()
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        rel_text = rel.as_posix()
        if is_sensitive_path(rel_text):
            continue
        try:
            if path.is_symlink():
                result[rel_text] = "symlink:" + os.readlink(path)
            elif path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                result[rel_text] = f"file:{path.stat().st_size}:{digest}"
        except (OSError, UnicodeError):
            result[rel_text] = "unreadable"
    return result


def workspace_fingerprint(manifest: dict[str, str]) -> str:
    return sha256_value(manifest)


def workspace_changes(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    before_keys = set(before)
    after_keys = set(after)
    return {
        "added": sorted(after_keys - before_keys),
        "deleted": sorted(before_keys - after_keys),
        "modified": sorted(key for key in before_keys & after_keys if before[key] != after[key]),
    }


def redact_tool_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    out = dict(args)
    path = str(out.get("path") or "")
    if name in {"write", "edit"} and is_sensitive_path(path):
        for key in ("content", "old_string", "new_string"):
            if key in out:
                out[key] = "<redacted>"
    return out


def redact_tool_output(name: str, args: dict[str, Any], output: str) -> str:
    if name in {"read", "web_fetch"} and is_sensitive_path(str(args.get("path") or args.get("url") or "")):
        return "<redacted sensitive tool output>"
    return output


def tool_audit(call: Any, result: Any) -> dict[str, Any]:
    return {
        "tool_call_id": str(call.call_id),
        "tool_name": str(call.name),
        "args_sha256": sha256_value(call.arguments),
        "decision": getattr(result, "safety_decision", "") or "",
        "decision_reason": getattr(result, "safety_reason", ""),
        "decision_source": getattr(result, "safety_source", ""),
        "is_error": bool(result.is_error),
        "blocked": bool(result.blocked),
    }
