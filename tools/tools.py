"""工具层：工具声明（name/description/parameters/执行器）+ 运行时。

ToolRuntime 负责参数校验、安全裁决、checkpoint 快照、截断，以及
并行/串行执行调度。bash 支持前台（超时杀）和后台（job_id + 轮询/杀）。"""

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
# 工具执行器签名：(args, workspace, on_update) → 输出文本
Executor = Callable[[dict[str, Any], Workspace, OnUpdate | None], str]
# 执行模式：并行（只读类）/串行（写类，避免互踩）
ExecutionMode = Literal["parallel", "sequential"]
# 前台 bash 默认超时（秒），超时杀进程。
FOREGROUND_TIMEOUT = 120


@dataclass
class _Job:
    """一个后台 bash 作业的运行时状态。"""

    job_id: str
    proc: subprocess.Popen[str]
    log_path: Path
    command: str
    notified: bool = False


# 全局后台作业表 + 锁（进程内单例，跨任务共享，atexit 时清场）。
JOBS: dict[str, _Job] = {}
JOBS_LOCK = threading.Lock()


@dataclass
class ToolSpec:
    """一个工具的完整声明：给模型看的 schema + 给运行时用的执行器与元信息。"""

    name: str
    description: str
    parameters: dict[str, Any]
    readonly: bool
    execute: Executor
    execution_mode: ExecutionMode = "sequential"
    # 输出截断策略：head（保头）/tail（保尾）/none。
    truncate: Literal["head", "tail", "none"] = "none"


def default_tools() -> list[ToolSpec]:
    """内置工具集：文件读/列/搜/写/编、bash、联网搜索/抓取。"""
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


def tool_schemas(tools: list[ToolSpec] | None = None) -> list[dict[str, Any]]:
    """把工具声明转成发往模型的 function schema 列表。"""
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
    """工具运行时：持有工作区/安全门/计划/harness，提供 execute_batch 入口。"""

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
        # plan 工具：非平凡任务先提交步骤、等批准再改文件。
        if not any(spec.name == "plan" for spec in specs):
            specs.append(
                ToolSpec(
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
                    execute=lambda a, w, u=None: self._plan(a),
                    execution_mode="sequential",
                )
            )
        if not any(spec.name == "harness" for spec in specs):
            # harness 工具：持久化 prompt/memory 笔记（持续学习）。
            specs.append(
                ToolSpec(
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
                    execute=lambda a, w, u=None: self._harness(a),
                    execution_mode="sequential",
                )
            )
        self.tools = {spec.name: spec for spec in specs}
        self.on_update = on_update
        self.checkpoints = CheckpointStore.for_workspace(workspace.root)
        self._proc: subprocess.Popen[str] | None = None
        self._task_id: str | None = None
        self._decisions: dict[str, Any] = {}
        # 给 bash 换上一个会记住当前进程的执行器（供 abort_running 杀）。
        if "bash" in self.tools and self.tools["bash"].execute is _bash:
            spec = self.tools["bash"]
            self.tools["bash"] = replace(
                spec,
                execute=lambda a, w, u=None: _bash(a, w, u, on_proc=self._set_proc),
            )
        self.tools.setdefault(
            "bash_poll",
            ToolSpec(
                name="bash_poll",
                description="Read output from a background bash job started with background=true. Pass the job_id.",
                parameters={
                    "type": "object",
                    "properties": {"job_id": {"type": "string"}},
                    "required": ["job_id"],
                    "additionalProperties": False,
                },
                readonly=True,
                execute=lambda a, w, u=None: _bash_poll(a),
                execution_mode="parallel",
                truncate="tail",
            ),
        )
        self.tools.setdefault(
            "bash_kill",
            ToolSpec(
                name="bash_kill",
                description="Kill a background bash job by job_id.",
                parameters={
                    "type": "object",
                    "properties": {"job_id": {"type": "string"}},
                    "required": ["job_id"],
                    "additionalProperties": False,
                },
                readonly=False,
                execute=lambda a, w, u=None: _bash_kill(a),
                execution_mode="sequential",
            ),
        )

    def _set_proc(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc

    @property
    def task_id(self) -> str | None:
        return self._task_id

    def begin_task(self) -> str:
        """开一个 checkpoint 任务，返回 task_id（之后的快照都归它）。"""
        self._task_id = self.checkpoints.begin_task()
        return self._task_id

    def abort_running(self) -> None:
        """杀当前正在跑的前台 bash 进程（Ctrl+C / /stop 时调）。"""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            _kill(proc)

    def _plan(self, args: dict[str, Any]) -> str:
        """plan 工具执行器：整体替换计划；被拒时转成 ValueError 供循环识别。"""
        try:
            return self.plan.replace(args.get("steps") or [])
        except PlanRejected as exc:
            raise ValueError(str(exc)) from exc

    def _harness(self, args: dict[str, Any]) -> str:
        """harness 工具执行器：转发给 HarnessStore.dispatch。"""
        return self.harness.dispatch(args)

    def execute(self, call: FunctionCall) -> ToolResult:
        return self.execute_batch([call])[0]

    def execute_batch(self, calls: list[FunctionCall]) -> list[ToolResult]:
        """执行一批工具调用：先逐个准备（校验+安全），再按执行模式分组调度。"""
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
            # 同组并行（只读类），单独串行（写类）。
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
        """执行前准备：解析/校验参数 + 安全裁决；被拒直接返回错误结果。"""
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
        """执行单个已放行的工具：先快照，再执行，再后处理（截断）。"""
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
            self._checkpoint(spec, args)   # 改文件前快照（供 undo）
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
        """上一个计划被拒后，拦截 write/edit，强制先提交新计划。"""
        if spec.name not in {"write", "edit"}:
            return None
        if self.plan.confirmed or not self.plan.rejected:
            return None
        return (
            "plan was rejected; call the plan tool with a revised step list "
            "before write/edit. The harness will ask y/N again."
        )

    def _checkpoint(self, spec: ToolSpec, args: dict[str, Any]) -> None:
        """为 write/edit/bash 存回滚快照；失败静默（不能阻塞主流程）。"""
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
        """输出后处理：按工具声明截断；bash 的 exit= 行作为保留前缀不被截掉。"""
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
    """宽容地校正参数类型：模型常把 array/object/boolean 发成字符串，这里转回。"""
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
    """按 schema 校验必填/未知字段/类型/enum；不合法抛 ValueError。"""
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
    """把可调度的调用按执行模式分组：连续的只读调用合并为一组并行，
    每个写调用单独一组串行。"""
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
    """从模型输出的 items 里拆出工具调用；参数 JSON 解析失败不报错，
    带 _parse_error 交给 prepare 阶段统一报。"""
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
    """read 工具：读文件（带 offset/limit），输出 相对路径 + 片段。"""
    del on_update
    offset = int(args.get("offset") or 1)
    limit = args.get("limit")
    limit_i = int(limit) if limit is not None else None
    chunk, _start = ws.read_text(str(args["path"]), offset=offset, limit=limit_i)
    rel = ws.rel(ws.resolve(str(args["path"])))
    return f"{rel}\n{chunk}"


def _ls(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    """ls 工具：列一个目录。"""
    del on_update
    entries = ws.list_dir(str(args.get("path") or "."))
    return "\n".join(entries) if entries else "(empty)"


def _grep(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    """grep 工具：正则搜文件内容（rg 优先），命中行转成相对路径。"""
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
                line = line[len(prefix) :]   # 剥掉工作区绝对前缀
            rel_hits.append(line)
        return "\n".join(rel_hits)
    return "(no matches)"


def _glob(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    """glob 工具：按模式找文件路径（不读内容），超 limit 加截断提示。"""
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
    """写前守卫：解析路径并拒绝敏感路径（密钥/.env 等）。"""
    target = ws.resolve(raw)
    rel = ws.rel(target)
    if is_sensitive_path(rel) or is_sensitive_path(str(target)):
        raise PermissionError(f"refusing to modify sensitive path {rel}")
    return target


def _write(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    """write 工具：创建/覆盖文件；写 .py 时顺带 py_compile 检查。"""
    del on_update
    content = str(args["content"])
    _guard_write(ws, str(args["path"]))
    path = ws.write_text(str(args["path"]), content)
    return f"wrote {ws.rel(path)} ({len(content.splitlines())} lines)" + _py_compile(path)


def _edit(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    """edit 工具：唯一匹配替换；保留原文件的换行符风格。"""
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
    """核心替换：old 必须唯一命中（除非 replace_all），0 次/多次都报错。"""
    if old == "":
        raise ValueError("old_string must not be empty")
    count = original.count(old)
    if count == 0:
        raise ValueError("old_string not found; read the file again")
    if count > 1 and not replace_all:
        raise ValueError(f"old_string matched {count} times; add context or set replace_all=true")
    return original.replace(old, new) if replace_all else original.replace(old, new, 1)


def _web_search(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    """web_search 工具：联网搜索。"""
    del ws, on_update
    try:
        return search_web(str(args["query"]), num_results=int(args.get("num_results") or 5))
    except WebError as exc:
        raise ValueError(str(exc)) from exc


def _web_fetch(args: dict[str, Any], ws: Workspace, on_update: OnUpdate | None = None) -> str:
    """web_fetch 工具：抓一个公网 URL 的文本。"""
    del ws, on_update
    try:
        return fetch_url(str(args["url"]))
    except WebError as exc:
        raise ValueError(str(exc)) from exc


def _py_compile(path: Any) -> str:
    """写/编 .py 后用 py_compile 快检语法，失败附在输出末尾。"""
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
    """bash 工具：前台（超时杀，双线程读 stdout/stderr）或后台（job_id）执行。"""
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
        on_proc(proc)   # 记下进程，供 abort_running 杀
    timeout = int(args.get("timeout") or FOREGROUND_TIMEOUT)
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def reader(stream: Any, bucket: list[str]) -> None:
        """逐行读一个流，累进 bucket 并回调 on_update（供 UI 实时显示）。"""
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
    """把进程转入后台作业：输出写日志文件，返回 job_id 提示。

    后台作业跨回合存活，由 bash_poll/bash_kill 管理，atexit 时统一清场。"""
    job_id = f"job_{token_hex(4)}"
    log_path = Path(ws.root) / ".wheel" / "outputs" / f"{job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    handle = log_path.open("a", encoding="utf-8")
    lock = threading.Lock()

    def reader(stream: Any) -> None:
        """逐行写日志（加锁，两个流共享一个文件句柄）。"""
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
        """进程退出后收尾：等读完、关日志。"""
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
    """bash_poll 工具：读后台作业当前状态与累计输出。"""
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
    """bash_kill 工具：杀后台作业。"""
    job = _get_job(str(args["job_id"]))
    if job.proc.poll() is None:
        _kill(job.proc)
        return f"killed {job.job_id}"
    return f"{job.job_id} already exited code={job.proc.returncode}"


def _get_job(job_id: str) -> _Job:
    """按 job_id 取作业，支持前缀匹配（唯一命中时）。"""
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
    """/jobs 命令用的作业列表文本。"""
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
    """取走尚未通知过的“作业退出”事件（每作业只报一次）。"""
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
    """杀所有后台作业（atexit / 会话结束时调）。"""
    with JOBS_LOCK:
        jobs = list(JOBS.values())
        JOBS.clear()
    for job in jobs:
        if job.proc.poll() is None:
            _kill(job.proc)


def _kill(proc: subprocess.Popen[str]) -> None:
    """SIGKILL 进程并短等回收；等不到也不报错。"""
    proc.kill()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _bash_result(code: str, stdout_parts: list[str], stderr_parts: list[str]) -> str:
    """拼前台 bash 结果：exit= + stdout + stderr。"""
    stdout = "".join(stdout_parts) or "(empty)"
    stderr = "".join(stderr_parts) or "(empty)"
    return f"exit={code}\nstdout:\n{stdout}\nstderr:\n{stderr}"


# 进程退出时杀光所有后台作业，不留孤儿进程。
atexit.register(kill_all_jobs)
