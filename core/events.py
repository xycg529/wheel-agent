from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

Listener = Callable[[dict[str, Any]], None]


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def new_run_id() -> str:
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    return f"{stamp}_{uuid.uuid4().hex[:8]}"


@dataclass
class EventBus:
    run_id: str
    run_dir: Path
    listeners: list[Listener] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.responses_path = self.run_dir / "responses.jsonl"
        self.meta_path = self.run_dir / "meta.json"

    @classmethod
    def create(cls, runs_dir: str | Path, run_id: str | None = None) -> "EventBus":
        rid = run_id or new_run_id()
        return cls(run_id=rid, run_dir=Path(runs_dir) / rid)

    def subscribe(self, listener: Listener) -> None:
        self.listeners.append(listener)

    def emit(self, type_: str, **data: Any) -> dict[str, Any]:
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
        row = {
            "turn": turn,
            "output": output,
            "usage": usage or {},
            "output_sha256": _json_hash(output),
        }
        if input_audit:
            row["input_audit"] = input_audit
        with self.responses_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def write_meta(self, **meta: Any) -> None:
        payload = {"run_id": self.run_id, **meta}
        self.meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        return _load_jsonl(self.events_path)

    def load_responses(self) -> list[dict[str, Any]]:
        if not self.responses_path.exists():
            return []
        return _load_jsonl(self.responses_path)



def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """JSONL reader that skips torn tail lines (crash between write and fsync)."""
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
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def list_run_ids(runs_dir: str | Path) -> list[str]:
    root = Path(runs_dir)
    if not root.exists():
        return []
    return sorted((p.name for p in root.iterdir() if p.is_dir()), reverse=True)


def load_run(runs_dir: str | Path, run_id: str) -> EventBus:
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
