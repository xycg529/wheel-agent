from __future__ import annotations

import json
import re
import time
from pathlib import Path
from secrets import token_hex
from typing import Any

from wheel_agent.safety import is_sensitive_path

MAX_BYTES = 1_000_000
SKIP_PARTS = {".wheel", ".wheel_runs", ".git"}
_FLAG = re.compile(r"^-")


class CheckpointStore:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._stack_path = self.dir / "stack.json"
        self._tasks_path = self.dir / "tasks.json"

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> "CheckpointStore":
        return cls(Path(workspace).resolve() / ".wheel" / "checkpoints")

    def begin_task(self) -> str:
        task_id = f"task_{int(time.time() * 1000)}_{token_hex(3)}"
        tasks = self._load_tasks()
        tasks["latest"] = task_id
        tasks.setdefault("items", {})[task_id] = []
        self._save_tasks(tasks)
        return task_id

    def latest_task_id(self) -> str | None:
        value = self._load_tasks().get("latest")
        return str(value) if value else None

    def snapshot(self, path: Path, *, tool: str, task_id: str | None = None) -> str | None:
        try:
            path = path.resolve()
        except OSError:
            return None
        if any(part in SKIP_PARTS for part in path.parts) or is_sensitive_path(str(path)):
            return None
        existed = path.is_file()
        content: str | None = None
        if existed:
            try:
                size = path.stat().st_size
            except OSError:
                return None
            if size > MAX_BYTES:
                return None
            try:
                data = path.read_bytes()
            except OSError:
                return None
            if b"\0" in data[:8192]:
                return None
            content = data.decode("utf-8", errors="replace")
        elif path.exists():
            return None
        cid = f"{int(time.time() * 1000)}_{token_hex(2)}"
        rec = {"id": cid, "path": str(path), "existed": existed, "content": content, "tool": tool}
        (self.dir / f"{cid}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        stack = self._load_stack()
        stack.append(cid)
        self._save_stack(stack[-200:])
        if task_id:
            tasks = self._load_tasks()
            tasks.setdefault("items", {}).setdefault(task_id, []).append(cid)
            self._save_tasks(tasks)
        return cid

    def snapshot_bash(self, command: str, resolve, task_id: str | None = None) -> None:
        if not re.search(r"\b(?:rm|mv)\b", command):
            return
        for tok in command.split():
            if _FLAG.match(tok) or tok in {"rm", "mv", "sudo", "--"}:
                continue
            tok = tok.strip("\"'")
            if not tok or tok in {"*", ".", ".."}:
                continue
            try:
                path = resolve(tok)
            except (OSError, PermissionError, ValueError):
                continue
            if path.is_file():
                self.snapshot(path, tool="bash", task_id=task_id)

    def rollback_task(self, task_id: str | None = None) -> list[str]:
        task_id = task_id or self.latest_task_id()
        if not task_id:
            return []
        tasks = self._load_tasks()
        ids = list((tasks.get("items") or {}).get(task_id) or [])
        stack = self._load_stack()
        msgs: list[str] = []
        for cid in reversed(ids):
            rec_path = self.dir / f"{cid}.json"
            try:
                rec = json.loads(rec_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            msgs.append(self._restore(rec))
            rec_path.unlink(missing_ok=True)
            if cid in stack:
                stack.remove(cid)
        self._save_stack(stack)
        tasks.setdefault("items", {}).pop(task_id, None)
        if tasks.get("latest") == task_id:
            items = tasks.get("items") or {}
            tasks["latest"] = next(reversed(items), None) if items else None
        self._save_tasks(tasks)
        return msgs

    def undo(self, n: int = 1) -> list[str]:
        n = max(1, int(n))
        stack = self._load_stack()
        msgs: list[str] = []
        for _ in range(n):
            if not stack:
                break
            cid = stack.pop()
            rec_path = self.dir / f"{cid}.json"
            try:
                rec = json.loads(rec_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            msgs.append(self._restore(rec))
            rec_path.unlink(missing_ok=True)
        self._save_stack(stack)
        return msgs

    def _load_tasks(self) -> dict[str, Any]:
        if not self._tasks_path.is_file():
            return {"latest": None, "items": {}}
        try:
            data = json.loads(self._tasks_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"latest": None, "items": {}}
        if not isinstance(data, dict):
            return {"latest": None, "items": {}}
        data.setdefault("items", {})
        return data

    def _save_tasks(self, tasks: dict[str, Any]) -> None:
        self._tasks_path.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")

    def _restore(self, rec: dict[str, Any]) -> str:
        path = Path(rec["path"])
        if rec.get("existed"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rec.get("content") or "", encoding="utf-8")
            return f"restored {path}"
        if path.is_file():
            path.unlink()
            return f"removed {path}"
        return f"skipped {path} (already gone)"

    def _load_stack(self) -> list[str]:
        if not self._stack_path.is_file():
            return []
        try:
            data = json.loads(self._stack_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [str(x) for x in data] if isinstance(data, list) else []

    def _save_stack(self, stack: list[str]) -> None:
        self._stack_path.write_text(json.dumps(stack), encoding="utf-8")
