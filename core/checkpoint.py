"""回滚基础设施：每次改文件/删文件前存快照，支持单步 undo 和整任务
回滚（/undo、/undo-task）。快照存工作区 .wheel/checkpoints/，有上限。"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from secrets import token_hex
from typing import Any

from wheel_agent.tools.safety import is_sensitive_path

# 单个文件快照的上限：再大就不存（回滚收益低，快照目录会爆）。
MAX_BYTES = 1_000_000
# 这些目录里的文件不进快照（工具自己的产物 / VCS 元数据）。
SKIP_PARTS = {".wheel", ".wheel_runs", ".git"}
# bash 命令里的选项（-rf 等），不是路径。
_FLAG = re.compile(r"^-")


class CheckpointStore:
    """快照存储：全局 undo 栈（stack.json）+ 按任务索引（tasks.json）。"""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._stack_path = self.dir / "stack.json"    # undo 栈：最近 200 个快照 ID
        self._tasks_path = self.dir / "tasks.json"    # 任务 → 快照 ID 列表的映射

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> "CheckpointStore":
        """工作区默认存储位置：<工作区>/.wheel/checkpoints/。"""
        return cls(Path(workspace).resolve() / ".wheel" / "checkpoints")

    def begin_task(self) -> str:
        """开一个新任务，返回 task_id；之后的快照都归到这个任务名下。"""
        task_id = f"task_{int(time.time() * 1000)}_{token_hex(3)}"
        tasks = self._load_tasks()
        tasks["latest"] = task_id
        tasks.setdefault("items", {})[task_id] = []
        self._save_tasks(tasks)
        return task_id

    def latest_task_id(self) -> str | None:
        """最近一个任务 ID（/undo-task 不带参数时用）。"""
        value = self._load_tasks().get("latest")
        return str(value) if value else None

    def snapshot(self, path: Path, *, tool: str, task_id: str | None = None) -> str | None:
        """给文件存一份快照（改前调用），返回快照 ID；不该存的情况返回 None。

        存的是文件完整内容（文本）；新文件存 existed=False + 空内容，
        回滚时就是删掉。"""
        try:
            path = path.resolve()
        except OSError:
            return None
        if any(part in SKIP_PARTS for part in path.parts) or is_sensitive_path(str(path)):
            return None   # 跳过工具产物目录和敏感路径（.env、密钥等）
        existed = path.is_file()
        content: str | None = None
        if existed:
            try:
                size = path.stat().st_size
            except OSError:
                return None
            if size > MAX_BYTES:
                # 超大文件不快照：全量副本会让 .wheel/checkpoints 暴涨，而回滚收益有限。
                return None
            try:
                data = path.read_bytes()
            except OSError:
                return None
            if b"\0" in data[:8192]:
                # 二进制嗅探：前 8KB 有 NUL 就当二进制，不存——
                # 按 UTF-8 文本存取会在恢复时损坏它。
                return None
            content = data.decode("utf-8", errors="replace")
        elif path.exists():
            return None
        cid = f"{int(time.time() * 1000)}_{token_hex(2)}"
        rec = {"id": cid, "path": str(path), "existed": existed, "content": content, "tool": tool}
        (self.dir / f"{cid}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        stack = self._load_stack()
        stack.append(cid)
        # 限制 undo 栈深度：200 个快照足够回滚，长会话里几千次编辑也不能无限涨。
        self._save_stack(stack[-200:])
        if task_id:
            tasks = self._load_tasks()
            tasks.setdefault("items", {}).setdefault(task_id, []).append(cid)
            self._save_tasks(tasks)
        return cid

    def snapshot_bash(self, command: str, resolve, task_id: str | None = None) -> None:
        """执行 bash 前先扫命令：含 rm/mv 时，对其文件参数存快照（尽力而为）。

        只能识别字面量路径参数；通配符/变量展开覆盖不到，这是已知限制。"""
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
        """把整个任务的改动回滚：按快照顺序倒序恢复，返回每个文件的恢复消息。"""
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
        # 刚回滚的是当前任务时，latest 指针退回上一个任务。
        if tasks.get("latest") == task_id:
            items = tasks.get("items") or {}
            tasks["latest"] = next(reversed(items), None) if items else None
        self._save_tasks(tasks)
        return msgs

    def undo(self, n: int = 1) -> list[str]:
        """单步撤销最近 n 个快照（/undo）。"""
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
        """读任务索引；文件损坏/缺失时返回空结构（回滚不能因为元数据坏了而崩溃）。"""
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
        """按快照恢复单个文件：改过的还原内容，新建的删掉，已不存在的跳过。"""
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
        """读 undo 栈；损坏时返回空列表。"""
        if not self._stack_path.is_file():
            return []
        try:
            data = json.loads(self._stack_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [str(x) for x in data] if isinstance(data, list) else []

    def _save_stack(self, stack: list[str]) -> None:
        self._stack_path.write_text(json.dumps(stack), encoding="utf-8")
