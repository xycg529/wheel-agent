"""事件流与记录：扁平的事件总线（TTY 渲染 / JSONL / 审计同源）
与运行记录的读写（events.jsonl、responses.jsonl、meta.json）。"""

from __future__ import annotations

import json
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# 事件订阅者的签名：收一个 dict，无返回。
Listener = Callable[[dict[str, Any]], None]


def _now() -> str:
    """本地时区 ISO 时间戳（事件与记录都用它）。"""
    return datetime.now().astimezone().isoformat()


def new_run_id() -> str:
    """运行 ID：时间戳 + 8 位随机后缀，同一毫秒内也不碰撞。"""
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    return f"{stamp}_{uuid.uuid4().hex[:8]}"


@dataclass
class EventBus:
    """一次运行的事件总线：一次 emit 同时写盘 + 喂订阅者，UI 只是事件流的一个视图。"""

    run_id: str
    run_dir: Path
    listeners: list[Listener] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 运行目录与三个记录文件：事件流、模型原始响应、收尾 meta。
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.responses_path = self.run_dir / "responses.jsonl"
        self.meta_path = self.run_dir / "meta.json"

    @classmethod
    def create(cls, runs_dir: str | Path, run_id: str | None = None) -> "EventBus":
        """新建运行；run_id 缺省自动生成（replay 传已有 id 复用目录）。"""
        rid = run_id or new_run_id()
        return cls(run_id=rid, run_dir=Path(runs_dir) / rid)

    def subscribe(self, listener: Listener) -> None:
        self.listeners.append(listener)

    def emit(self, type_: str, **data: Any) -> dict[str, Any]:
        """发一个事件：先落盘再喂订阅者（副本遍历，订阅者内退订也安全）。"""
        event = {"type": type_, "run_id": self.run_id, "ts": _now(), **data}
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        for listener in list(self.listeners):
            listener(event)
        return event

    def record_response(
        self,
        turn: int,
        output: list[dict[str, Any]],
        usage: dict[str, int] | None = None,
        *,
        input_audit: dict[str, Any] | None = None,
    ) -> None:
        """记录模型原始响应：replay 用它把模型换成录制脚本重跑。"""
        row = {
            "turn": turn,
            "output": output,
            "usage": usage or {},
            "output_sha256": _json_hash(output),   # 响应哈希：replay 对比时校验录制未变
        }
        if input_audit:
            row["input_audit"] = input_audit
        with self.responses_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def write_meta(self, **meta: Any) -> None:
        payload = {"run_id": self.run_id, **meta}
        self.meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_events(self) -> list[dict[str, Any]]:
        return _load_jsonl(self.events_path) if self.events_path.exists() else []

    def load_responses(self) -> list[dict[str, Any]]:
        return _load_jsonl(self.responses_path) if self.responses_path.exists() else []



def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """JSONL 读取：跳过空白行和非法 JSON 的尾行（崩溃留下的半行）。"""
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _json_hash(value: Any) -> str:
    """规范 JSON 哈希（键排序）：同样内容一定得到同样哈希。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def list_run_ids(runs_dir: str | Path) -> list[str]:
    """列出全部运行 ID，新的在前（/replay 无参数时的选择列表）。"""
    root = Path(runs_dir)
    if not root.exists():
        return []
    return sorted((p.name for p in root.iterdir() if p.is_dir()), reverse=True)


def load_run(runs_dir: str | Path, run_id: str) -> EventBus:
    """按 ID 找回运行；支持前缀匹配与按 session_id 查找（meta.json 里记录的）。"""
    root = Path(runs_dir)
    path = root / run_id
    if path.exists():
        return EventBus(run_id=run_id, run_dir=path)
    matches = sorted(root.glob(f"{run_id}*"), key=lambda p: p.stat().st_mtime, reverse=True)
    dirs = [p for p in matches if p.is_dir()]
    if len(dirs) == 1:
        return EventBus(run_id=dirs[0].name, run_dir=dirs[0])
    session_hits: list[Path] = []
    if root.exists():
        for child in root.iterdir():
            meta = child / "meta.json"
            if not meta.exists():
                continue
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            sid = str(payload.get("session_id") or "")
            if sid == run_id or sid.startswith(run_id):
                session_hits.append(child)
    if session_hits:
        session_hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        chosen = session_hits[0]
        return EventBus(run_id=chosen.name, run_dir=chosen)
    raise FileNotFoundError(f"run not found: {path}")
