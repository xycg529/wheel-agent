from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wheel_agent.types import Decision, FunctionCall, SafetyVerdict


AskFn = Callable[[str], bool]

READ_ONLY = {"read", "ls", "grep", "glob", "web_search", "web_fetch", "bash_poll"}
OWN_JOBS = {"bash_kill"}
KEY_NAMES = {"id_rsa", "id_ed25519", "id_ecdsa", "credentials.json", "auth.json", "secrets.json"}
ENV_ALLOW = {".env.example", ".env.sample"}
WRAPPERS = {"time", "nohup", "command", "nice", "env", "busybox"}
ASK_ACTIONS = {
    "delete",
    "move",
    "chmod",
    "chown",
    "git_reset",
    "git_clean",
    "git_force_push",
    "kill",
    "sql_drop",
    "crontab",
}
DENY_ACTIONS = {"destroy", "privilege"}
PIPE_REMOTE = re.compile(r"(?:curl|wget)\s+[^\n|]*\|\s*(?:sh|bash)")
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PY_DELETE = re.compile(
    r"""(?:os\.(?:remove|unlink|rmdir)|shutil\.rmtree)\(\s*['"]([^'"]+)['"]"""
    r"""|Path\(\s*['"]([^'"]+)['"]\s*\)\.unlink"""
)


@dataclass
class BashIntent:
    action: str = "allow"
    paths: list[str] = field(default_factory=list)
    escaped: list[str] = field(default_factory=list)


class SafetyGate:
    def __init__(
        self,
        *,
        interactive: bool = True,
        ask: AskFn | None = None,
        memory: set[tuple[str, ...]] | None = None,
        workspace: str | Path | None = None,
    ):
        self.interactive = interactive
        self.ask = ask
        self.memory: set[tuple[str, ...]] = memory if memory is not None else set()
        self.workspace = Path(workspace).resolve() if workspace is not None else None

    def review(self, call: FunctionCall) -> SafetyVerdict:
        verdict = classify(call, self.workspace)
        if verdict.decision == "allow":
            return verdict
        keys = approval_keys(call, self.workspace)
        if verdict.decision == "ask" and keys and all(key in self.memory for key in keys):
            return SafetyVerdict("allow", "remembered approval", source="memory")
        if verdict.decision == "deny":
            return verdict
        if verdict.decision == "ask":
            if not self.interactive:
                return SafetyVerdict("deny", f"non-interactive: {verdict.reason}", source=verdict.source)
            prompt = f"{verdict.reason}\n{short_args(call)}\nAllow this action?"
            if self.ask and self.ask(prompt):
                self.memory.update(keys)
                return SafetyVerdict("allow", "user approved", source="user")
            return SafetyVerdict("deny", "user declined or no prompt available", source="user")
        return verdict


def approval_key(call: FunctionCall, workspace: str | Path | None = None) -> tuple[str, ...]:
    keys = approval_keys(call, workspace)
    return keys[0] if keys else (call.name,)


def approval_keys(call: FunctionCall, workspace: str | Path | None = None) -> list[tuple[str, ...]]:
    if call.name != "bash":
        return [(call.name, json.dumps(call.arguments, sort_keys=True, ensure_ascii=False))]
    command = str(call.arguments.get("command") or call.arguments.get("cmd") or "").strip()
    intent = parse_bash_intent(command, workspace)
    if intent.action == "allow":
        return []
    paths = intent.paths or ["*"]
    return [("bash", intent.action, path) for path in paths]


def is_sensitive_path(path: str) -> bool:
    posix = path.replace("\\", "/").strip()
    parts = [p for p in posix.split("/") if p and p not in {".", ".."}]
    if ".git" in parts:
        return True
    name = parts[-1] if parts else posix.rsplit("/", 1)[-1]
    if name in KEY_NAMES:
        return True
    if name.endswith((".pem", ".p12", ".pfx", ".key")):
        return True
    if name == ".env":
        return True
    if name.startswith(".env.") and name not in ENV_ALLOW:
        return True
    return False


def classify(call: FunctionCall, workspace: str | Path | None = None) -> SafetyVerdict:
    if call.name in READ_ONLY:
        path = str(call.arguments.get("path") or "")
        if path and _escapes(path, workspace):
            return SafetyVerdict("deny", f"path escapes workspace: {path}", source="rules")
        return SafetyVerdict("allow", "read-only tool", source="rules")
    if call.name in {"write", "edit"}:
        path = str(call.arguments.get("path") or "")
        if is_sensitive_path(path):
            return SafetyVerdict("deny", f"refusing to modify sensitive path {path}", source="rules")
        if _escapes(path, workspace):
            return SafetyVerdict("deny", f"path escapes workspace: {path}", source="rules")
        return SafetyVerdict("allow", "workspace mutation is allowed", source="rules")
    if call.name in {"plan", "harness"} | OWN_JOBS:
        return SafetyVerdict("allow", "workspace mutation is allowed", source="rules")
    if call.name == "bash":
        command = str(call.arguments.get("command") or call.arguments.get("cmd") or "")
        return classify_bash(command, workspace)
    return SafetyVerdict("ask", f"unknown tool {call.name}", source="rules")


def classify_bash(command: str, workspace: str | Path | None = None) -> SafetyVerdict:
    text = command.strip()
    if not text:
        return SafetyVerdict("deny", "empty command", source="rules")
    if _is_destroy(text):
        return SafetyVerdict("deny", "destructive machine command", source="rules")
    if re.search(r"\bsudo\b", text):
        return SafetyVerdict("deny", "sudo escalates privileges", source="rules")
    intent = parse_bash_intent(text, workspace)
    if intent.action in DENY_ACTIONS:
        return SafetyVerdict("deny", f"{intent.action} is not allowed", source="rules")
    if intent.escaped:
        return SafetyVerdict("deny", f"path escapes workspace: {intent.escaped[0]}", source="rules")
    if intent.action in ASK_ACTIONS:
        sensitive = [path for path in intent.paths if is_sensitive_path(path)]
        if sensitive:
            return SafetyVerdict("deny", f"refusing to {intent.action} sensitive path {sensitive[0]}", source="rules")
        target = ", ".join(intent.paths) if intent.paths else "workspace"
        return SafetyVerdict("ask", f"{intent.action} {target}", source="rules")
    return SafetyVerdict("allow", "command not on danger list", source="rules")


def parse_bash_intent(command: str, workspace: str | Path | None = None) -> BashIntent:
    actions: list[str] = []
    paths: list[str] = []
    escaped: list[str] = []
    for segment in _segments(command):
        piece = _parse_segment(segment, workspace)
        actions.append(piece.action)
        paths.extend(piece.paths)
        escaped.extend(piece.escaped)
    action = "allow"
    for candidate in ("destroy", "privilege", "delete", "move", "git_reset", "git_clean", "git_force_push", "chmod", "chown", "kill", "sql_drop", "crontab"):
        if candidate in actions:
            action = candidate
            break
    return BashIntent(action=action, paths=_unique(paths), escaped=_unique(escaped))


def short_args(call: FunctionCall) -> str:
    raw = json.dumps(call.arguments, ensure_ascii=False)
    return raw if len(raw) <= 240 else raw[:237] + "..."


def _is_destroy(text: str) -> bool:
    if PIPE_REMOTE.search(text):
        return True
    if re.search(r":\(\)\s*\{", text):
        return True
    if re.search(r"\b(?:mkfs|wipefs)\b", text):
        return True
    if re.search(r"\b(?:reboot|shutdown|poweroff|halt)\b", text):
        return True
    if re.search(r">\s*/dev/", text):
        return True
    if re.search(r"/etc/passwd|/etc/shadow", text):
        return True
    if re.search(r"\bdd\b", text) and "/dev/" in text:
        return True
    return False


def _segments(command: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command.startswith("&&", i) or command.startswith("||", i):
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
            i += 2
            continue
        if ch in ";\n" or (ch == "|" and not command.startswith("||", i)):
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    piece = "".join(buf).strip()
    if piece:
        parts.append(piece)
    return parts


def _parse_segment(segment: str, workspace: str | Path | None) -> BashIntent:
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        tokens = segment.split()
    tokens = _strip_wrappers(tokens)
    if not tokens:
        return BashIntent()
    if tokens[0] == "sudo":
        return BashIntent(action="privilege")
    verb = Path(tokens[0]).name
    args = tokens[1:]
    if verb in {"rm", "rmdir", "unlink", "shred"}:
        paths, escaped = _collect_paths(args, workspace)
        return BashIntent("delete", paths, escaped)
    if verb == "mv":
        paths, escaped = _collect_paths(args, workspace)
        return BashIntent("move", paths, escaped)
    if verb == "chmod":
        paths, escaped = _collect_paths(_skip_mode(args), workspace)
        return BashIntent("chmod", paths, escaped)
    if verb == "chown":
        paths, escaped = _collect_paths(_skip_mode(args), workspace)
        return BashIntent("chown", paths, escaped)
    if verb == "git":
        return _parse_git(args)
    if verb in {"python", "python3"}:
        return _parse_python(args, workspace)
    if verb == "find":
        return _parse_find(args, workspace)
    if verb in {"kill", "killall"}:
        return BashIntent("kill")
    if verb == "crontab":
        return BashIntent("crontab")
    if re.search(r"\bdrop\s+(?:table|database)\b", segment, re.I):
        return BashIntent("sql_drop")
    return BashIntent()


def _strip_wrappers(tokens: list[str]) -> list[str]:
    out = list(tokens)
    while out and ASSIGN.match(out[0]):
        out.pop(0)
    while out and out[0] in WRAPPERS:
        out.pop(0)
        while out and ASSIGN.match(out[0]):
            out.pop(0)
    return out


def _skip_mode(args: list[str]) -> list[str]:
    if not args:
        return args
    if args[0].startswith("-") and len(args) > 1:
        return args[1:]
    if re.match(r"^[0-7]{3,4}$", args[0]) or ":" in args[0] or args[0].startswith("u") or args[0].startswith("g"):
        return args[1:]
    return args


def _parse_git(args: list[str]) -> BashIntent:
    i = 0
    while i < len(args):
        if args[i] == "-C" and i + 1 < len(args):
            i += 2
            continue
        if args[i].startswith("-") and args[i] not in {"-"}:
            i += 1
            continue
        break
    sub = args[i] if i < len(args) else ""
    rest = args[i + 1 :]
    if sub == "reset" and any(item == "--hard" or item.startswith("--hard") or item == "--merge" for item in rest):
        return BashIntent("git_reset", ["."])
    if sub == "clean":
        return BashIntent("git_clean", ["."])
    if sub == "push" and any("force" in item for item in rest):
        return BashIntent("git_force_push", ["."])
    return BashIntent()


def _parse_python(args: list[str], workspace: str | Path | None) -> BashIntent:
    blob = " ".join(args)
    if "-c" in args:
        idx = args.index("-c")
        if idx + 1 < len(args):
            blob = args[idx + 1]
    if not re.search(r"os\.(?:remove|unlink|rmdir)|shutil\.rmtree|\.unlink\s*\(", blob):
        return BashIntent()
    found = [a or b for a, b in PY_DELETE.findall(blob)]
    paths, escaped = _resolve_many(found, workspace)
    return BashIntent("delete", paths, escaped)


def _parse_find(args: list[str], workspace: str | Path | None) -> BashIntent:
    if not any(item in {"-delete", "-exec"} or item.startswith("-exec") for item in args):
        return BashIntent()
    raw: list[str] = []
    for item in args:
        if item.startswith("-"):
            break
        raw.append(item)
    if not raw:
        raw = ["."]
    paths, escaped = _resolve_many(raw, workspace)
    return BashIntent("delete", paths, escaped)


def _collect_paths(args: list[str], workspace: str | Path | None) -> tuple[list[str], list[str]]:
    raw = [item for item in args if item != "--" and not item.startswith("-")]
    return _resolve_many(raw, workspace)


def _resolve_many(raw: list[str], workspace: str | Path | None) -> tuple[list[str], list[str]]:
    paths: list[str] = []
    escaped: list[str] = []
    for item in raw:
        rel, outside = _resolve_target(item, workspace)
        if outside:
            escaped.append(item)
        elif rel is not None:
            paths.append(rel)
    return paths, escaped


def _escapes(path: str, workspace: str | Path | None) -> bool:
    if not path:
        return False
    _rel, outside = _resolve_target(path, workspace)
    return outside


def _resolve_target(raw: str, workspace: str | Path | None) -> tuple[str | None, bool]:
    text = raw.strip()
    if not text or text.startswith("$"):
        return None, False
    posix = text.replace("\\", "/")
    if workspace is None:
        if posix.startswith("/") or posix.startswith("~") or posix.startswith("..") or (
            len(posix) > 1 and posix[1] == ":"
        ):
            return None, True
        cleaned = posix
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        return cleaned or ".", False
    root = Path(workspace).resolve()
    try:
        candidate = Path(text)
        candidate = candidate.resolve() if candidate.is_absolute() else (root / text).resolve()
    except OSError:
        return posix.lstrip("./") or ".", False
    try:
        rel = candidate.relative_to(root)
    except ValueError:
        return None, True
    mapped = rel.as_posix()
    return mapped if mapped != "." else ".", False


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
