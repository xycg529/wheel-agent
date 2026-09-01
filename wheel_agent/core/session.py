"""会话持久化：JSONL 日志 + 会话树（每条记录带 parent_id）。

当前对话 = 从根到 leaf 的路径；紧凑是叠加层（overlay）不销毁原树；
/tree、/fork 靠移动 leaf 指针实现零拷贝分支。"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wheel_agent.core.compact import is_summary_item
from wheel_agent.core.events import _now, new_run_id
from wheel_agent.core.model import item_text
from wheel_agent.core.plan import PlanStore
from wheel_agent.core.types import Item, Usage

# 会话目录相对工作区的位置；文件命名 <session_id>.jsonl。
SESSION_DIR = ".wheel/sessions"
# 日志格式版本：v1 无 parent_id（加载时按写入顺序重建线性链）。
CURRENT_VERSION = 2


def _nid() -> str:
    """8 位随机条目 ID。"""
    return uuid.uuid4().hex[:8]


def session_dir(workspace: str | Path) -> Path:
    """会话目录（不存在则建）。"""
    path = Path(workspace).resolve() / SESSION_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class SessionEntry:
    """树里的一个节点：一条消息 + 父指针（parent_id 为 None 即根）。"""

    id: str
    parent_id: str | None
    item: Item


@dataclass
class CompactOverlay:
    """紧凑叠加层：摘要替换到 after_id 之前的全部历史。

    原树不销毁——跳分支/回看旧内容时仍完整可用。"""

    summary: Item
    after_id: str


@dataclass
class Session:
    """一个会话的完整状态：消息树 + 视图 + 紧凑叠加 + 计划 + 缓存纪元。"""

    session_id: str
    path: Path
    cwd: str
    items: list[Item] = field(default_factory=list)   # 当前视图（发给模型的列表）
    turn_offset: int = 0    # 已用回合数（turn 编号续计用）
    usage: Usage = field(default_factory=Usage)        # 会话累计用量
    header: dict[str, Any] = field(default_factory=dict)
    entries: dict[str, SessionEntry] = field(default_factory=dict)   # id → 节点
    order: list[str] = field(default_factory=list)       # 写入顺序（持久化水位用）
    leaf_id: str | None = None    # 当前叶子：当前对话 = 根→leaf 路径
    overlay: CompactOverlay | None = None
    plan: PlanStore = field(default_factory=PlanStore)
    cache_epoch: int = 0   # 缓存纪元：历史变形成时自增，重算 prompt_cache_key
    approvals: list[list[str]] = field(default_factory=list)   # 本会话已批准过的 bash 前缀
    compactions: int = 0
    last_compact: dict[str, Any] = field(default_factory=dict)
    _saved: int = 0   # 已落盘的 order 长度（增量追加水位）

    @property
    def cache_key(self) -> str:
        """prompt 缓存分区键：纪元一变，前缀缓存重新计。"""
        return f"{self.session_id}:{self.cache_epoch}"

    @classmethod
    def create(cls, workspace: str | Path) -> "Session":
        """新建空会话（文件在首次 persist 时才写）。"""
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
        """从 JSONL 恢复完整状态：头 + 全部节点 + 末尾 meta（用量/leaf/叠加/计划）。

        加载时还会自愈：对“调了工具但结果没落盘就崩了”的孤儿调用，
        补一条 interrupted 输出，避免下次请求被 API 拒（400）。"""
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
                # 损坏的尾行：persist() 在 write 与 fsync 之间崩了会留下半行，
                # 和其他日志读取一样跳过。
                continue
            kind = entry.get("type")
            if kind in {"item", "entry"}:
                item = entry.get("item")
                if not item:
                    continue
                eid = str(entry.get("id") or _nid())
                parent = entry.get("parent_id", entry.get("parentId"))
                # v1 日志没有 parent_id：按写入顺序重建线性链。
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
            # 崩在“工具已发出、结果未落盘”之间：API 会拒这种孤儿调用，
            # 补一条 interrupted 输出让会话能干净恢复，而不是 400。
            for item in extras:
                session.append_item(item, to_view=True)
            session.persist()
        session._saved = len(session.order)
        return session

    @classmethod
    def _session_files(cls, workspace: str | Path) -> list[Path]:
        """会话文件列表，新的在前（按 mtime）。"""
        return sorted(session_dir(workspace).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    @classmethod
    def latest(cls, workspace: str | Path) -> "Session | None":
        """最近一个非空会话（REPL 启动时自动恢复用）。"""
        for path in cls._session_files(workspace):
            if not cls.file_is_empty(path):
                return cls.load(path)
        return None

    @classmethod
    def list_previews(cls, workspace: str | Path, width: int = 48) -> list[tuple[str, str]]:
        """会话列表 + 首条用户消息预览（/sessions 显示用），跳过空文件。"""
        rows: list[tuple[str, str]] = []
        for path in cls._session_files(workspace):
            preview = first_user_preview_from_path(path, width)
            if preview == "(empty)":
                continue
            rows.append((path.stem, preview))
        return rows

    @classmethod
    def load_id(cls, workspace: str | Path, session_id: str) -> "Session":
        """按 ID 加载，支持前缀匹配（唯一命中时）。"""
        path = session_dir(workspace) / f"{session_id}.jsonl"
        if not path.exists():
            matches = list(session_dir(workspace).glob(f"{session_id}*.jsonl"))
            if len(matches) == 1:
                path = matches[0]
            else:
                raise FileNotFoundError(f"session not found: {session_id}")
        return cls.load(path)

    def append_item(self, item: Item, *, to_view: bool = True) -> str:
        """追加一条消息到树上；返回条目 ID。

        树永远记；视图（self.items）只在调用者没自己加过时才加——
        run_agent 把 self.items 当作活动 prompt 列表直接 append，
        所以它的 _push 传 to_view=False 避免重复。"""
        eid = _nid()
        node = SessionEntry(id=eid, parent_id=self.leaf_id, item=item)
        self.entries[eid] = node
        self.order.append(eid)
        self.leaf_id = eid
        if to_view:
            self.items.append(item)
        return eid

    def path_ids(self) -> list[str]:
        """根→leaf 的条目 ID 链（当前对话）。带环保护防脏数据死循环。"""
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
        """当前视图：路径上的消息；有紧凑叠加时用摘要替换叠加点之前的部分。"""
        ids = self.path_ids()
        if self.overlay and self.overlay.after_id in ids:
            idx = ids.index(self.overlay.after_id)
            return [self.overlay.summary, *[self.entries[i].item for i in ids[idx:]]]
        return [self.entries[i].item for i in ids]

    def apply_compact(self, compacted: list[Item]) -> None:
        """把紧凑结果应用到会话：设叠加层 + 自增缓存纪元 + 更新视图。

        原树节点不变（只替换保留后缀里被改写的节点），
        跳分支时仍能回到未压缩的完整历史。"""
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
            after_id = self.path_ids()[-1]   # 定位不到保留后缀起点时，叠加到全路径之后
        if after_id:
            self.overlay = CompactOverlay(summary=summary, after_id=after_id)
            ids = self.path_ids()
            idx = ids.index(after_id)
            self._sync_ids(ids[idx:], compacted[1:])
        self.cache_epoch += 1
        self.items[:] = compacted

    def invalidate_cache(self) -> None:
        """历史被带外修改（如 refine 应用了编辑）：自增缓存纪元并全量重写文件，
        让下次模型调用用新键。"""
        self.cache_epoch += 1
        self.persist(rewrite=True)

    def _sync_path_items(self, view: list[Item]) -> None:
        if self.overlay:
            return
        self._sync_ids(self.path_ids(), view)

    def _sync_ids(self, ids: list[str], view: list[Item]) -> None:
        """把视图里的消息同步回树节点（逐 ID 对齐，缺节点跳过）。"""
        for eid, item in zip(ids, view):
            node = self.entries.get(eid)
            if node is None:
                continue
            self.entries[eid] = SessionEntry(id=eid, parent_id=node.parent_id, item=item)

    def set_leaf(self, entry_id: str) -> None:
        """把 leaf 指针移到指定条目（/tree 跳转的核心，零拷贝）。"""
        if entry_id not in self.entries:
            raise KeyError(f"unknown entry {entry_id}")
        self.leaf_id = entry_id
        if self.overlay and self.overlay.after_id not in self.path_ids():
            self.overlay = None   # 跳出了叠加层生效范围：回到未压缩视图
        self.items[:] = self.view_items()

    def fork(self, entry_id: str | None = None) -> None:
        """从指定条目（缺省最后一个 user 消息）分叉：leaf 移过去，之后追加即新分支。"""
        target = entry_id or self._last_user_id()
        if not target:
            raise ValueError("nothing to fork")
        self.set_leaf(target)

    def tree_rows(self) -> list[dict[str, Any]]:
        """/tree 命令的行数据：每个 user 消息一行，带深度、是否在路径上、是否 leaf。"""
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
        """路径上最后一个 user 消息的条目 ID。"""
        for eid in reversed(self.path_ids()):
            if self.entries[eid].item.get("role") == "user":
                return eid
        return None

    @staticmethod
    def file_is_empty(path: Path) -> bool:
        """文件里没有任何消息条目（只有头/meta）算空。"""
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
        """删掉全部空会话文件，返回删除数。"""
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
        """落盘。默认增量追加：只写水位之后的条目 + 一条 meta（便宜，
        尾行损坏也可恢复）。历史变形时（叠加/分支/新文件）全量重写——
        只追加的日志只能长大，不能变形。写后 flush+fsync 保证断电不丢。"""
        if not self.entries and not self.items:
            return
        if not self.entries and self.items:
            self._rebuild_linear()   # 旧数据直接塞 items 的：重建线性树
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
        """items → 线性树（parent 串成链），用于无树数据的旧会话。"""
        prev: str | None = None
        for item in self.items:
            eid = _nid()
            self.entries[eid] = SessionEntry(id=eid, parent_id=prev, item=item)
            self.order.append(eid)
            prev = eid
        self.leaf_id = prev

    def _rewrite(self) -> None:
        """全量重写：头 + 全部条目 + 尾部 meta，写后 fsync。"""
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
        """一个节点的 JSONL 行。"""
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
        """尾部 meta 行：所有可变状态的快照（每次 persist 覆盖重放）。"""
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
        """真实用户轮数（摘要消息不算）。"""
        return sum(1 for item in self.items if item.get("role") == "user" and not is_summary_item(item))


def preview_user_text(text: str, width: int = 48) -> str:
    """会话列表的预览：skill 展开文本缩成 /skill:name，长文截断。"""
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
    """找出“有调用无结果”的孤儿工具调用（崩溃残留），为它们造 interrupted 输出。"""
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
    """不加载整个会话，只扫文件找第一条用户消息做预览。"""
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
