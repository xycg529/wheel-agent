from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wheel_agent.compact import is_summary_item
from wheel_agent.events import _now, new_run_id
from wheel_agent.model import item_text
from wheel_agent.plan import PlanStore
from wheel_agent.types import Item, Usage

SESSION_DIR = ".wheel/sessions"
CURRENT_VERSION = 2


def _nid() -> str:
    return uuid.uuid4().hex[:8]


def session_dir(workspace: str | Path) -> Path:
    path = Path(workspace).resolve() / SESSION_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class SessionEntry:
    id: str
    parent_id: str | None
    item: Item


@dataclass
class CompactOverlay:
    summary: Item
    after_id: str


@dataclass
class Session:
    session_id: str
    path: Path
    cwd: str
    items: list[Item] = field(default_factory=list)
    turn_offset: int = 0
    usage: Usage = field(default_factory=Usage)
    header: dict[str, Any] = field(default_factory=dict)
    entries: dict[str, SessionEntry] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    leaf_id: str | None = None
    overlay: CompactOverlay | None = None
    plan: PlanStore = field(default_factory=PlanStore)
    cache_epoch: int = 0
    approvals: list[list[str]] = field(default_factory=list)
    compactions: int = 0
    last_compact: dict[str, Any] = field(default_factory=dict)
    _saved: int = 0

    @property
    def cache_key(self) -> str:
        return f"{self.session_id}:{self.cache_epoch}"

    @classmethod
    def create(cls, workspace: str | Path) -> "Session":
        root = Path(workspace).resolve()
        sid = new_run_id()
        path = session_dir(root) / f"{sid}.jsonl"
        header = {
            "type": "session",
            "version": CURRENT_VERSION,
            "id": sid,
            "cwd": str(root),
            "timestamp": _now(),
        }
        return cls(session_id=sid, path=path, cwd=str(root), header=header)

    @classmethod
    def load(cls, path: str | Path) -> "Session":
        path = Path(path)
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            raise ValueError(f"empty session file: {path}")
        try:
            header = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt session header: {path}") from exc
        version = int(header.get("version") or 1)
        entries: dict[str, SessionEntry] = {}
        order: list[str] = []
        items: list[Item] = []
        turn_offset = 0
        usage = Usage()
        leaf_id: str | None = None
        overlay: CompactOverlay | None = None
        plan = PlanStore()
        cache_epoch = 0
        approvals: list[list[str]] = []
        compactions = 0
        last_compact: dict[str, Any] = {}
        prev_id: str | None = None
        for line in lines[1:]:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # Torn tail: persist() can crash between write() and fsync(),
                # leaving a truncated last line. Skip it like the other journal
                # readers (file_is_empty, first_user_preview_from_path) do.
                continue
            kind = entry.get("type")
            if kind in {"item", "entry"}:
                item = entry.get("item")
                if not item:
                    continue
                eid = str(entry.get("id") or _nid())
                parent = entry.get("parent_id", entry.get("parentId"))
                if parent is None and version == 1:
                    parent = prev_id
                node = SessionEntry(id=eid, parent_id=str(parent) if parent else None, item=item)
                entries[eid] = node
                order.append(eid)
                items.append(item)
                prev_id = eid
                leaf_id = eid
            elif kind == "meta":
                turn_offset = int(entry.get("turn_offset") or turn_offset)
                if entry.get("usage"):
                    usage = Usage.from_dict(entry["usage"])
                if entry.get("leaf_id"):
                    leaf_id = str(entry["leaf_id"])
                if entry.get("overlay"):
                    ov = entry["overlay"]
                    overlay = CompactOverlay(summary=ov["summary"], after_id=str(ov["after_id"]))
                if entry.get("plan"):
                    plan.steps = list(entry["plan"].get("steps") or [])
                    plan.confirmed = bool(entry["plan"].get("confirmed"))
                    plan.rejected = bool(entry["plan"].get("rejected"))
                if entry.get("cache_epoch") is not None:
                    cache_epoch = int(entry["cache_epoch"])
                if entry.get("approvals"):
                    approvals = [list(row) for row in entry["approvals"] if isinstance(row, list)]
                if entry.get("compactions") is not None:
                    compactions = int(entry["compactions"])
                if isinstance(entry.get("last_compact"), dict):
                    last_compact = dict(entry["last_compact"])
        session = cls(
            session_id=str(header.get("id") or path.stem),
            path=path,
            cwd=str(header.get("cwd") or path.parent),
            items=items,
            turn_offset=turn_offset,
            usage=usage,
            header=header,
            entries=entries,
            order=order,
            leaf_id=leaf_id,
            overlay=overlay,
            plan=plan,
            cache_epoch=cache_epoch,
            approvals=approvals,
            compactions=compactions,
            last_compact=last_compact,
            _saved=len(order),
        )
        session.items = session.view_items()
        extras = unpaired_function_call_outputs(session.items)
        if extras:
            for item in extras:
                session.append_item(item, to_view=True)
            session.persist()
        session._saved = len(session.order)
        return session

    @classmethod
    def latest(cls, workspace: str | Path) -> "Session | None":
        files = sorted(session_dir(workspace).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            if not cls.file_is_empty(path):
                return cls.load(path)
        return None

    @classmethod
    def list_previews(cls, workspace: str | Path, width: int = 48) -> list[tuple[str, str]]:
        files = sorted(session_dir(workspace).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        rows: list[tuple[str, str]] = []
        for path in files:
            preview = first_user_preview_from_path(path, width)
            if preview == "(empty)":
                continue
            rows.append((path.stem, preview))
        return rows

    @classmethod
    def load_id(cls, workspace: str | Path, session_id: str) -> "Session":
        path = session_dir(workspace) / f"{session_id}.jsonl"
        if not path.exists():
            matches = list(session_dir(workspace).glob(f"{session_id}*.jsonl"))
            if len(matches) == 1:
                path = matches[0]
            else:
                raise FileNotFoundError(f"session not found: {session_id}")
        return cls.load(path)

    def append_item(self, item: Item, *, to_view: bool = True) -> str:
        eid = _nid()
        node = SessionEntry(id=eid, parent_id=self.leaf_id, item=item)
        self.entries[eid] = node
        self.order.append(eid)
        self.leaf_id = eid
        if to_view:
            self.items.append(item)
        return eid

    def path_ids(self) -> list[str]:
        ids: list[str] = []
        cur = self.leaf_id
        seen: set[str] = set()
        while cur and cur not in seen:
            seen.add(cur)
            if cur not in self.entries:
                break
            ids.append(cur)
            cur = self.entries[cur].parent_id
        ids.reverse()
        return ids

    def view_items(self) -> list[Item]:
        ids = self.path_ids()
        if self.overlay and self.overlay.after_id in ids:
            idx = ids.index(self.overlay.after_id)
            return [self.overlay.summary, *[self.entries[i].item for i in ids[idx:]]]
        return [self.entries[i].item for i in ids]

    def apply_compact(self, compacted: list[Item]) -> None:
        if not compacted:
            self.items = compacted
            return
        summary = compacted[0] if is_summary_item(compacted[0]) else None
        if summary is None:
            self._sync_path_items(compacted)
            self.items[:] = compacted
            return
        after_item = compacted[1] if len(compacted) > 1 else None
        after_id = None
        if after_item is not None:
            for eid in reversed(self.path_ids()):
                if self.entries[eid].item is after_item or self.entries[eid].item == after_item:
                    after_id = eid
                    break
        if after_id is None and self.path_ids():
            after_id = self.path_ids()[-1]
        if after_id:
            self.overlay = CompactOverlay(summary=summary, after_id=after_id)
            ids = self.path_ids()
            idx = ids.index(after_id)
            self._sync_ids(ids[idx:], compacted[1:])
        self.cache_epoch += 1
        self.items[:] = compacted

    def _sync_path_items(self, view: list[Item]) -> None:
        if self.overlay:
            return
        self._sync_ids(self.path_ids(), view)

    def _sync_ids(self, ids: list[str], view: list[Item]) -> None:
        for eid, item in zip(ids, view):
            node = self.entries.get(eid)
            if node is None:
                continue
            self.entries[eid] = SessionEntry(id=eid, parent_id=node.parent_id, item=item)

    def set_leaf(self, entry_id: str) -> None:
        if entry_id not in self.entries:
            raise KeyError(f"unknown entry {entry_id}")
        self.leaf_id = entry_id
        if self.overlay and self.overlay.after_id not in self.path_ids():
            self.overlay = None
        self.items[:] = self.view_items()

    def fork(self, entry_id: str | None = None) -> None:
        target = entry_id or self._last_user_id()
        if not target:
            raise ValueError("nothing to fork")
        self.set_leaf(target)

    def tree_rows(self) -> list[dict[str, Any]]:
        path = set(self.path_ids())
        rows: list[dict[str, Any]] = []
        for eid in self.order:
            node = self.entries[eid]
            item = node.item
            if item.get("role") != "user":
                continue
            text = str(item.get("content") or "")
            if is_summary_item(item):
                label = "(summary)"
            else:
                label = text.replace("\n", " ")[:80]
            depth = 0
            cur = node.parent_id
            while cur and cur in self.entries:
                if self.entries[cur].item.get("role") == "user":
                    depth += 1
                cur = self.entries[cur].parent_id
            rows.append(
                {
                    "id": eid,
                    "depth": depth,
                    "on_path": eid in path,
                    "leaf": eid == self.leaf_id or (self.leaf_id in path and eid == self._last_user_id()),
                    "label": label,
                }
            )
        return rows

    def _last_user_id(self) -> str | None:
        for eid in reversed(self.path_ids()):
            if self.entries[eid].item.get("role") == "user":
                return eid
        return None

    @staticmethod
    def file_is_empty(path: Path) -> bool:
        try:
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            return False
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") in {"item", "entry"}:
                return False
        return True

    @classmethod
    def purge_empty(cls, workspace: str | Path) -> int:
        removed = 0
        for path in list(session_dir(workspace).glob("*.jsonl")):
            if not cls.file_is_empty(path):
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def persist(self, rewrite: bool = False) -> None:
        if not self.entries and not self.items:
            return
        if not self.entries and self.items:
            self._rebuild_linear()
        elif self.entries and self.items and self.overlay is None:
            self._sync_path_items(self.items)
        if rewrite or not self.path.exists() or self.overlay is not None:
            self._rewrite()
            return
        with self.path.open("a", encoding="utf-8") as fh:
            unsaved = self.order[self._saved :] if self._saved <= len(self.order) else self.order
            for eid in unsaved:
                node = self.entries[eid]
                fh.write(self._entry_line(node))
            fh.write(self._meta_line())
            fh.flush()
            os.fsync(fh.fileno())
        self._saved = len(self.order)

    def _rebuild_linear(self) -> None:
        prev: str | None = None
        for item in self.items:
            eid = _nid()
            self.entries[eid] = SessionEntry(id=eid, parent_id=prev, item=item)
            self.order.append(eid)
            prev = eid
        self.leaf_id = prev

    def _rewrite(self) -> None:
        header = dict(self.header or {})
        header.update(
            {
                "type": "session",
                "version": CURRENT_VERSION,
                "id": self.session_id,
                "cwd": self.cwd,
                "timestamp": header.get("timestamp") or _now(),
            }
        )
        self.header = header
        lines = [json.dumps(header, ensure_ascii=False)]
        for eid in self.order:
            lines.append(self._entry_line(self.entries[eid]).rstrip("\n"))
        lines.append(self._meta_line().rstrip("\n"))
        payload = "\n".join(lines) + "\n"
        self.path.write_text(payload, encoding="utf-8")
        with self.path.open("rb") as fh:
            os.fsync(fh.fileno())
        self._saved = len(self.order)

    def _entry_line(self, node: SessionEntry) -> str:
        return (
            json.dumps(
                {
                    "type": "entry",
                    "id": node.id,
                    "parent_id": node.parent_id,
                    "item": node.item,
                    "timestamp": _now(),
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    def _meta_line(self) -> str:
        payload: dict[str, Any] = {
            "type": "meta",
            "turn_offset": self.turn_offset,
            "usage": self.usage.as_dict(),
            "leaf_id": self.leaf_id,
            "timestamp": _now(),
        }
        if self.overlay:
            payload["overlay"] = {"summary": self.overlay.summary, "after_id": self.overlay.after_id}
        payload["cache_epoch"] = self.cache_epoch
        payload["compactions"] = self.compactions
        if self.last_compact:
            payload["last_compact"] = self.last_compact
        if self.approvals:
            payload["approvals"] = self.approvals
        if self.plan.steps or self.plan.rejected:
            payload["plan"] = {
                "steps": self.plan.steps,
                "confirmed": self.plan.confirmed,
                "rejected": self.plan.rejected,
            }
        return json.dumps(payload, ensure_ascii=False) + "\n"

    def user_turns(self) -> int:
        return sum(1 for item in self.items if item.get("role") == "user" and not is_summary_item(item))


def preview_user_text(text: str, width: int = 48) -> str:
    body = (text or "").strip()
    if body.startswith("<skill"):
        name = ""
        marker = "name=\""
        start = body.find(marker)
        if start >= 0:
            start += len(marker)
            end = body.find("\"", start)
            if end > start:
                name = body[start:end]
        rest = body.split("</skill>", 1)[-1].strip() if "</skill>" in body else ""
        body = f"/skill:{name}" if name else "/skill"
        if rest:
            body = f"{body} {rest}"
    body = " ".join(body.split())
    if not body:
        return "(empty)"
    if len(body) <= width:
        return body
    return body[:width] + "…"


def unpaired_function_call_outputs(items: list[Item]) -> list[Item]:
    pending: dict[str, Item] = {}
    for item in items:
        kind = item.get("type")
        cid = str(item.get("call_id") or "")
        if kind == "function_call" and cid:
            pending[cid] = item
        elif kind == "function_call_output" and cid:
            pending.pop(cid, None)
    extras: list[Item] = []
    for cid, call in pending.items():
        name = str(call.get("name") or "tool")
        extras.append(
            {
                "type": "function_call_output",
                "call_id": cid,
                "output": (
                    f"interrupted: {name} was dispatched and the outcome is unknown. "
                    "Do not blindly retry side-effecting tools; inspect the workspace first."
                ),
            }
        )
    return extras


def first_user_preview_from_path(path: Path, width: int = 48) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "(unreadable)"
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") not in {"item", "entry"}:
            continue
        item = entry.get("item") or {}
        if item.get("role") != "user":
            continue
        text = item_text(item)
        if is_summary_item(item):
            continue
        return preview_user_text(text, width)
    return "(empty)"
