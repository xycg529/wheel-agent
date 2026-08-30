from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from pathlib import Path
from secrets import token_hex
from typing import Any, Literal

from wheel_agent.core.checkpoint import CheckpointStore
from wheel_agent.harness.harness import HarnessStore
from wheel_agent.core.plan import PlanRejected, PlanStore
from wheel_agent.tools.rgfiles import DEFAULT_LIMIT, glob_files, grep_files
from wheel_agent.tools.safety import SafetyGate, is_sensitive_path
from wheel_agent.core.truncate import GREP_MAX_LINE_LENGTH, apply
from wheel_agent.core.types import FunctionCall, ToolResult
from wheel_agent.tools.web import WebError, fetch_url, search_web
from wheel_agent.tools.workspace import Workspace

OnUpdate = Callable[[str], None]
Executor = Callable[[dict[str, Any], Workspace, OnUpdate | None], str]
ExecutionMode = Literal["parallel", "sequential"]
FOREGROUND_TIMEOUT = 120


@dataclass
class _Job:
    job_id: str
    proc: subprocess.Popen[str]
    log_path: Path
    command: str
    notified: bool = False


JOBS: dict[str, _Job] = {}
JOBS_LOCK = threading.Lock()


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    readonly: bool
    execute: Executor
    execution_mode: ExecutionMode = "sequential"
    truncate: Literal["head", "tail", "none"] = "none"


def default_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="read",
            description="Read a text file. Paths are relative to the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace"},
                    "offset": {"type": "integer", "description": "1-based start line"},
                    "limit": {"type": "integer", "description": "Max number of lines"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            readonly=True,
            execute=_read,
            execution_mode="parallel",
            truncate="head",
        ),
        ToolSpec(
            name="ls",
            description="List files and directories in the workspace.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path, default ."}},
                "required": [],
                "additionalProperties": False,
            },
            readonly=True,
            execute=_ls,
            execution_mode="parallel",
        ),
        ToolSpec(
            name="grep",
            description=(
                "Search file contents with a regex. Uses ripgrep when installed. "
                "Optional glob filters which files (e.g. '*.py'). Not for listing filenames — use glob."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "File or directory to search"},
                    "glob": {"type": "string", "description": "Filename filter, e.g. '*.py'"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            readonly=True,
            execute=_grep,
            execution_mode="parallel",
        ),
        ToolSpec(
            name="glob",
            description=(
                "Find files by glob pattern (e.g. '**/*.py', '*.md'). Uses ripgrep --files when installed. "
                "Returns paths only; does not read contents. ls lists one directory."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern relative to path"},
                    "path": {"type": "string", "description": "Directory to search, default ."},
                    "limit": {"type": "integer"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            readonly=True,
            execute=_glob,
            execution_mode="parallel",
        ),
        ToolSpec(
            name="write",
            description="Create or overwrite a file with the given contents.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            readonly=False,
            execute=_write,
            execution_mode="sequential",
        ),
        ToolSpec(
            name="edit",
            description=(
                "Edit a file with unique old_string/new_string. "
                "If old_string is not unique, set replace_all=true or add more context."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
            readonly=False,
            execute=_edit,
            execution_mode="sequential",
        ),
        ToolSpec(
            name="bash",
            description=(
                "Run a shell command inside the workspace. "
                "Foreground timeout default 120s (process is killed). "
                "For install/tests/servers, set background=true: returns job_id immediately. "
                "Then STOP and tell the user the job_id. Do not poll in this turn."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": "Foreground timeout seconds, default 120"},
                    "background": {"type": "boolean", "description": "Start and return a job_id; use bash_poll"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            readonly=False,
            execute=_bash,
            execution_mode="sequential",
            truncate="tail",
        ),
        ToolSpec(
            name="web_search",
            description="Search the public web (Exa). Returns titles, URLs, and short snippets.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "num_results": {"type": "integer", "description": "Default 5, max 10"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            readonly=True,
            execute=_web_search,
            execution_mode="parallel",
            truncate="head",
        ),
        ToolSpec(
            name="web_fetch",
            description="Fetch a public http(s) URL and return text (HTML stripped). No private/localhost hosts.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
                "additionalProperties": False,
            },
            readonly=True,
            execute=_web_fetch,
            execution_mode="parallel",
            truncate="head",
        ),
    ]


def _harness_spec(execute: Executor) -> ToolSpec:
    return ToolSpec(
        name="harness",
        description=(
            "Persist a durable prompt note or memory in the continual harness. "
            "kind=prompt is a behavioral policy; kind=memory is a fact/preference/decision. "
            "Default scope is this session. Set global=true only for stable cross-session lessons. "
            "Do not store one-off task progress. action=list shows current entries."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "update", "delete"]},
                "kind": {"type": "string", "enum": ["prompt", "memory"]},
                "id": {"type": "string"},
                "title": {"type": "string"},
                "content": {"type": "string"},
                "path": {"type": "string", "description": "Optional grouping path"},
                "global": {"type": "boolean", "description": "Write the cross-session store"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        readonly=False,
        execute=execute,
        execution_mode="sequential",
    )


def _plan_spec(execute: Executor) -> ToolSpec:
    return ToolSpec(
        name="plan",
        description=(
            "Replace the current task plan with the full list of steps. Send EVERY step each call. "
            "Statuses: pending, in_progress, done. At most one in_progress. "
            "For non-trivial work, think through the steps first, submit the plan, wait for approval, then edit files. "
            "After approval, mark the current step in_progress, do it, then mark it done before the next. "
            "If a plan was rejected, submit a revised plan before write/edit; do not start implementing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
                        },
                        "required": ["content", "status"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["steps"],
            "additionalProperties": False,
        },
        readonly=False,
        execute=execute,
        execution_mode="sequential",
    )


def _poll_spec(execute: Executor) -> ToolSpec:
    return ToolSpec(
        name="bash_poll",
        description="Read output from a background bash job started with background=true. Pass the job_id.",
        parameters={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        readonly=True,
        execute=execute,
        execution_mode="parallel",
        truncate="tail",
    )


def _kill_spec(execute: Executor) -> ToolSpec:
    return ToolSpec(
        name="bash_kill",
        description="Kill a background bash job by job_id.",
        parameters={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        readonly=False,
        execute=execute,
        execution_mode="sequential",
    )


def tool_schemas(tools: list[ToolSpec] | None = None) -> list[dict[str, Any]]:
    specs = tools or default_tools()
    return [
        {
            "type": "function",
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        }
        for spec in specs
    ]


class ToolRuntime:
    def __init__(
        self,
        workspace: Workspace,
        safety: SafetyGate,
        tools: list[ToolSpec] | None = None,
        on_update: OnUpdate | None = None,
        plan: PlanStore | None = None,
        harness: HarnessStore | None = None,
    ):
        self.workspace = workspace
        self.safety = safety
        self.plan = plan or PlanStore(ask=safety.ask, interactive=safety.interactive)
        self.harness = harness or HarnessStore.for_workspace(
            workspace.root, interactive=safety.interactive
        )
        specs = list(tools or default_tools())
        if not any(spec.name == "plan" for spec in specs):
            specs.append(_plan_spec(lambda a, w, u=None: self._plan(a)))
        if not any(spec.name == "harness" for spec in specs):
            specs.append(_harness_spec(lambda a, w, u=None: self._harness(a)))
        self.tools = {spec.name: spec for spec in specs}
        self.on_update = on_update
        self.checkpoints = CheckpointStore.for_workspace(workspace.root)
        self._proc: subprocess.Popen[str] | None = None
        self._task_id: str | None = None
        self._decisions: dict[str, Any] = {}
        if "bash" in self.tools and self.tools["bash"].execute is _bash:
            spec = self.tools["bash"]
            self.tools["bash"] = replace(
                spec,
                execute=lambda a, w, u=None: _bash(a, w, u, on_proc=self._set_proc),
            )
        self.tools.setdefault("bash_poll", _poll_spec(lambda a, w, u=None: _bash_poll(a)))
        self.tools.setdefault("bash_kill", _kill_spec(lambda a, w, u=None: _bash_kill(a)))

    def _set_proc(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc

    @property
    def task_id(self) -> str | None:
        return self._task_id

    def begin_task(self) -> str:
        self._task_id = self.checkpoints.begin_task()
        return self._task_id

    def abort_running(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            _kill(proc)

    def _plan(self, args: dict[str, Any]) -> str:
        try:
            return self.plan.replace(args.get("steps") or [])
        except PlanRejected as exc:
            raise ValueError(str(exc)) from exc

    def _harness(self, args: dict[str, Any]) -> str:
        return self.harness.dispatch(args)

    def execute(self, call: FunctionCall) -> ToolResult:
        return self.execute_batch([call])[0]

    def execute_batch(self, calls: list[FunctionCall]) -> list[ToolResult]:
        prepared: list[tuple[FunctionCall, ToolSpec | None, dict[str, Any] | None, ToolResult | None]] = []
        for call in calls:
            spec, args, early = self._prepare(call)
            prepared.append((call, spec, args, early))

        results: list[ToolResult | None] = [early for *_, early in prepared]
        runnable = [
            (idx, call, spec, args)
            for idx, (call, spec, args, early) in enumerate(prepared)
            if early is None and spec is not None and args is not None
        ]
        for group in _execution_groups(runnable):
            sequential = group[0][2].execution_mode == "sequential" if group else True
            if sequential or len(group) <= 1:
                for idx, call, spec, args in group:
                    results[idx] = self._run(spec, call, args)
            else:
                with ThreadPoolExecutor(max_workers=min(8, len(group) or 1)) as pool:
                    futs = {pool.submit(self._run, spec, call, args): idx for idx, call, spec, args in group}
                    wait(futs)
                    for fut, idx in futs.items():
                        results[idx] = fut.result()
        return [item if item is not None else ToolResult(call.call_id, call.name, "internal error", True) for item, (call, *_rest) in zip(results, prepared)]

    def _prepare(self, call: FunctionCall) -> tuple[ToolSpec | None, dict[str, Any] | None, ToolResult | None]:
        spec = self.tools.get(call.name)
        if spec is None:
            return None, None, ToolResult(call.call_id, call.name, f"unknown tool: {call.name}", is_error=True)
        try:
            args = prepare_arguments(spec, call.arguments)
            validate_arguments(spec, args)
        except Exception as exc:
            return spec, None, ToolResult(call.call_id, call.name, f"invalid arguments: {exc}", is_error=True)
        verdict = self.safety.review(FunctionCall(call.call_id, call.name, args, call.raw_arguments))
        self._decisions[call.call_id] = verdict
        if verdict.decision == "deny":
            return spec, args, ToolResult(
                call.call_id,
                call.name,
                f"blocked by safety ({verdict.source}): {verdict.reason}",
                is_error=True,
                blocked=True,
                safety_decision=verdict.decision,
                safety_reason=verdict.reason,
                safety_source=verdict.source,
            )
        return spec, args, None

    def _run(self, spec: ToolSpec, call: FunctionCall, args: dict[str, Any]) -> ToolResult:
        verdict = self._decisions.get(call.call_id)
        fields = {
            "safety_decision": getattr(verdict, "decision", ""),
            "safety_reason": getattr(verdict, "reason", ""),
            "safety_source": getattr(verdict, "source", ""),
        }
        blocked = self._rejected_plan_block(spec)
        if blocked:
            return ToolResult(call.call_id, spec.name, blocked, is_error=True, **fields)
        try:
            self._checkpoint(spec, args)
            output = spec.execute(args, self.workspace, self.on_update)
            output = self._after(spec, output)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            return ToolResult(
                call.call_id,
                call.name,
                f"{type(exc).__name__}: {exc}",
                is_error=True,
                **fields,
            )
        return ToolResult(call.call_id, call.name, output, **fields)

    def _rejected_plan_block(self, spec: ToolSpec) -> str | None:
        if spec.name not in {"write", "edit"}:
            return None
        if self.plan.confirmed or not self.plan.rejected:
            return None
        return (
            "plan was rejected; call the plan tool with a revised step list "
            "before write/edit. The harness will ask y/N again."
        )

    def _checkpoint(self, spec: ToolSpec, args: dict[str, Any]) -> None:
        try:
            if spec.name in {"write", "edit"}:
                raw = str(args.get("path") or "")
                if raw:
                    target = _guard_write(self.workspace, raw)
                    self.checkpoints.snapshot(target, tool=spec.name, task_id=self._task_id)
            elif spec.name == "bash":
                self.checkpoints.snapshot_bash(
                    str(args.get("command") or ""), self.workspace.resolve, task_id=self._task_id
                )
        except Exception:
            return

    def _after(self, spec: ToolSpec, output: str) -> str:
        if spec.truncate == "head":
            return apply(output, self.workspace.root, tail=False)
        if spec.truncate == "tail":
            prefix = ""
            if output.startswith("exit="):
                first, _, rest = output.partition("\n")
                prefix = first + "\n"
                output = rest
                return apply(prefix + output, self.workspace.root, tail=True, keep_prefix=prefix)
            return apply(output, self.workspace.root, tail=True)
        return output


def prepare_arguments(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
    if "_parse_error" in args:
        raise ValueError(args["_parse_error"])
    props = spec.parameters.get("properties") or {}
    out = dict(args)
    for key, schema in props.items():
        if key not in out:
            continue
        value = out[key]
        expected = schema.get("type") if isinstance(schema, dict) else None
        if isinstance(value, str) and expected in {"array", "object"}:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                continue
            if expected == "array" and isinstance(parsed, list):
                out[key] = parsed
            elif expected == "object" and isinstance(parsed, dict):
                out[key] = parsed
        if isinstance(value, str) and expected == "boolean":
            low = value.strip().lower()
            if low in {"true", "false", "1", "0", "yes", "no"}:
                out[key] = low in {"true", "1", "yes"}
    return out


def validate_arguments(spec: ToolSpec, args: dict[str, Any]) -> None:
    schema = spec.parameters or {}
    props = schema.get("properties") or {}
    for key in schema.get("required") or []:
        if key not in args:
            raise ValueError(f"missing {key}")
    if schema.get("additionalProperties") is False:
        unknown = next((key for key in args if key not in props), None)
        if unknown is not None:
            raise ValueError(f"unexpected {unknown}")
    for key, value in args.items():
        prop = props.get(key) if isinstance(props, dict) else None
        if not isinstance(prop, dict):
            continue
        expected = prop.get("type")
        if expected == "string" and not isinstance(value, str):
            raise ValueError(f"{key} should be string")
        if expected == "integer" and type(value) is not int:
            raise ValueError(f"{key} should be integer")
        if expected == "boolean" and type(value) is not bool:
            raise ValueError(f"{key} should be boolean")
        if expected == "array" and not isinstance(value, list):
            raise ValueError(f"{key} should be array")
        if expected == "object" and not isinstance(value, dict):
            raise ValueError(f"{key} should be object")
        enum = prop.get("enum")
        if enum is not None and value not in enum:
            raise ValueError(f"{key} must be one of {enum}")


def _execution_groups(
    runnable: list[tuple[int, FunctionCall, ToolSpec, dict[str, Any]]],
) -> list[list[tuple[int, FunctionCall, ToolSpec, dict[str, Any]]]]:
    groups: list[list[tuple[int, FunctionCall, ToolSpec, dict[str, Any]]]] = []
    current: list[tuple[int, FunctionCall, ToolSpec, dict[str, Any]]] = []
    current_seq: bool | None = None
    for row in runnable:
        sequential = row[2].execution_mode == "sequential"
        if current_seq is None:
            current_seq = sequential
            current = [row]
            if sequential:
                groups.append(current)
                current = []
                current_seq = None
            continue
        if sequential:
            if current:
                groups.append(current)
                current = []
            groups.append([row])
            current_seq = None
            continue
        current.append(row)
    if current:
        groups.append(current)
    return groups


def parse_function_calls(output: list[dict[str, Any]]) -> list[FunctionCall]:
    calls: list[FunctionCall] = []
    for item in output:
        if item.get("type") != "function_call":
            continue
        raw = item.get("arguments") or "{}"
        if not isinstance(raw, str):
            raw = json.dumps(raw)
        try:
            args = json.loads(raw) if raw.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
        except Exception as exc:
            args = {"_parse_error": str(exc), "_raw": raw}
        calls.append(
            FunctionCall(
                call_id=str(item.get("call_id") or item.get("id") or f"call_{len(calls)}"),
                name=str(item.get("name") or ""),
                arguments=args,
                raw_arguments=raw,
            )
        )
    return calls


def _read(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    del on_update
    offset = int(args.get("offset") or 1)
    limit = args.get("limit")
    limit_i = int(limit) if limit is not None else None
    chunk, _start = ws.read_text(str(args["path"]), offset=offset, limit=limit_i)
    rel = ws.rel(ws.resolve(str(args["path"])))
    return f"{rel}\n{chunk}"


def _ls(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    del on_update
    entries = ws.list_dir(str(args.get("path") or "."))
    return "\n".join(entries) if entries else "(empty)"


def _grep(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    del on_update
    target = ws.resolve(str(args.get("path") or "."))
    glob = str(args["glob"]) if args.get("glob") else None
    hits = grep_files(
        target,
        str(args["pattern"]),
        glob=glob,
        limit=DEFAULT_LIMIT,
        max_line=GREP_MAX_LINE_LENGTH,
    )
    if hits:
        rel_hits = []
        prefix = str(ws.root) + os.sep
        for line in hits:
            if line.startswith(prefix):
                line = line[len(prefix) :]
            rel_hits.append(line)
        return "\n".join(rel_hits)
    return "(no matches)"


def _glob(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    del on_update
    root = ws.resolve(str(args.get("path") or "."))
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {ws.rel(root)}")
    limit = int(args.get("limit") or DEFAULT_LIMIT)
    hits = glob_files(root, str(args["pattern"]), limit=limit)
    if not hits:
        return "(no matches)"
    lines = [ws.rel(path) for path in hits]
    if len(hits) >= limit:
        lines.append("...[truncated]")
    return "\n".join(lines)


def _guard_write(ws: Workspace, raw: str) -> Path:
    target = ws.resolve(raw)
    rel = ws.rel(target)
    if is_sensitive_path(rel) or is_sensitive_path(str(target)):
        raise PermissionError(f"refusing to modify sensitive path {rel}")
    return target


def _write(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    del on_update
    content = str(args["content"])
    _guard_write(ws, str(args["path"]))
    path = ws.write_text(str(args["path"]), content)
    return f"wrote {ws.rel(path)} ({len(content.splitlines())} lines)" + _py_compile(path)


def _edit(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    del on_update
    path = _guard_write(ws, str(args["path"]))
    if not path.exists():
        raise FileNotFoundError(f"file not found: {ws.rel(path)}")
    original = path.read_text(encoding="utf-8")
    newline = "\n" if original.endswith("\n") else ""
    updated = _edit_replace(
        original,
        str(args["old_string"]),
        str(args["new_string"]),
        bool(args.get("replace_all")),
    )
    if not updated.endswith("\n") and newline:
        updated += "\n"
    path.write_text(updated, encoding="utf-8")
    return f"edited {ws.rel(path)}" + _py_compile(path)


def _edit_replace(original: str, old: str, new: str, replace_all: bool) -> str:
    if old == "":
        raise ValueError("old_string must not be empty")
    count = original.count(old)
    if count == 0:
        raise ValueError("old_string not found; read the file again")
    if count > 1 and not replace_all:
        raise ValueError(f"old_string matched {count} times; add context or set replace_all=true")
    return original.replace(old, new) if replace_all else original.replace(old, new, 1)


def _web_search(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    del ws, on_update
    try:
        return search_web(str(args["query"]), num_results=int(args.get("num_results") or 5))
    except WebError as exc:
        raise ValueError(str(exc)) from exc


def _web_fetch(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    del ws, on_update
    try:
        return fetch_url(str(args["url"]))
    except WebError as exc:
        raise ValueError(str(exc)) from exc


def _py_compile(path: Any) -> str:
    target = Path(path)
    if target.suffix != ".py":
        return ""
    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", str(target)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode == 0:
        return ""
    err = (proc.stderr or proc.stdout or "py_compile failed").strip()
    return f"\n\npy_compile failed:\n{err}"


def _bash(
    args: dict[str, Any],
    ws: Workspace,
    on_update: OnUpdate | None = None,
    on_proc: Callable[[subprocess.Popen[str]], None] | None = None,
) -> str:
    command = str(args["command"])
    background = bool(args.get("background"))
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=ws.root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env={**os.environ, "PWD": str(ws.root)},
    )
    if background:
        return _start_job(proc, command, ws)
    if on_proc:
        on_proc(proc)
    timeout = int(args.get("timeout") or FOREGROUND_TIMEOUT)
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def reader(stream: Any, bucket: list[str]) -> None:
        try:
            for line in iter(stream.readline, ""):
                bucket.append(line)
                if on_update:
                    on_update(line)
        finally:
            stream.close()

    t_out = threading.Thread(target=reader, args=(proc.stdout, stdout_parts), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, stderr_parts), daemon=True)
    t_out.start()
    t_err.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        t_out.join(timeout=1)
        t_err.join(timeout=1)
        return _bash_result("timeout", stdout_parts, stderr_parts)
    except KeyboardInterrupt:
        _kill(proc)
        t_out.join(timeout=1)
        t_err.join(timeout=1)
        raise
    t_out.join(timeout=1)
    t_err.join(timeout=1)
    code = proc.returncode if proc.returncode is not None else -1
    return _bash_result(str(code), stdout_parts, stderr_parts)


def _start_job(proc: subprocess.Popen[str], command: str, ws: Workspace) -> str:
    job_id = f"job_{token_hex(4)}"
    log_path = Path(ws.root) / ".wheel" / "outputs" / f"{job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    handle = log_path.open("a", encoding="utf-8")
    lock = threading.Lock()

    def reader(stream: Any) -> None:
        try:
            for line in iter(stream.readline, ""):
                with lock:
                    handle.write(line)
                    handle.flush()
        finally:
            stream.close()

    readers = [
        threading.Thread(target=reader, args=(proc.stdout,), daemon=True),
        threading.Thread(target=reader, args=(proc.stderr,), daemon=True),
    ]
    for thread in readers:
        thread.start()

    def closer() -> None:
        proc.wait()
        for thread in readers:
            thread.join(timeout=1)
        with lock:
            handle.close()

    threading.Thread(target=closer, daemon=True).start()
    job = _Job(job_id=job_id, proc=proc, log_path=log_path, command=command)
    with JOBS_LOCK:
        JOBS[job_id] = job
    rel = ws.rel(log_path)
    return (
        f"background job_id={job_id} pid={proc.pid} log={rel}\n"
        "Job keeps running after this turn. Tell the user this job_id and stop. "
        "Do not bash_poll in a loop now."
    )


def _bash_poll(args: dict[str, Any]) -> str:
    job = _get_job(str(args["job_id"]))
    log = ""
    try:
        log = job.log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    code = job.proc.poll()
    if code is None:
        status = f"status=running pid={job.proc.pid}"
    else:
        status = f"status=exited code={code}"
    body = log if log else "(no output yet)"
    return f"{status}\n{body}"


def _bash_kill(args: dict[str, Any]) -> str:
    job = _get_job(str(args["job_id"]))
    if job.proc.poll() is None:
        _kill(job.proc)
        return f"killed {job.job_id}"
    return f"{job.job_id} already exited code={job.proc.returncode}"


def _get_job(job_id: str) -> _Job:
    key = str(job_id)
    with JOBS_LOCK:
        job = JOBS.get(key)
        if job is None:
            matches = [item for item in JOBS.values() if item.job_id.startswith(key)]
            job = matches[0] if len(matches) == 1 else None
    if job is None:
        raise ValueError(f"unknown job_id {job_id}")
    return job


def format_jobs() -> str:
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    if not jobs:
        return "(no jobs)"
    lines = []
    for job in jobs:
        code = job.proc.poll()
        status = "running" if code is None else f"exit {code}"
        cmd = " ".join(job.command.split())[:80]
        lines.append(f"{job.job_id}  {status:<10}  {cmd}")
    return "\n".join(lines)


def kill_job(job_id: str) -> str:
    return _bash_kill({"job_id": job_id})


def drain_job_events() -> list[str]:
    events: list[str] = []
    with JOBS_LOCK:
        for job in JOBS.values():
            code = job.proc.poll()
            if code is None or job.notified:
                continue
            job.notified = True
            events.append(f"job {job.job_id} exited {code}")
    return events


def kill_all_jobs() -> None:
    with JOBS_LOCK:
        jobs = list(JOBS.values())
        JOBS.clear()
    for job in jobs:
        if job.proc.poll() is None:
            _kill(job.proc)


def _kill(proc: subprocess.Popen[str]) -> None:
    proc.kill()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _bash_result(code: str, stdout_parts: list[str], stderr_parts: list[str]) -> str:
    stdout = "".join(stdout_parts) or "(empty)"
    stderr = "".join(stderr_parts) or "(empty)"
    return f"exit={code}\nstdout:\n{stdout}\nstderr:\n{stderr}"


atexit.register(kill_all_jobs)
