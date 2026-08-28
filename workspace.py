from __future__ import annotations

from pathlib import Path


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str | Path) -> Path:
        raw = Path(path)
        candidate = raw.resolve() if raw.is_absolute() else (self.root / raw).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError(f"path escapes workspace: {path}") from exc
        return candidate

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    def read_text(self, path: str, offset: int = 1, limit: int | None = None) -> tuple[str, int]:
        target = self.resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"file not found: {self.rel(target)}")
        if not target.is_file():
            raise IsADirectoryError(f"not a file: {self.rel(target)}")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, offset)
        end = len(lines) if limit is None else min(len(lines), start - 1 + max(limit, 0))
        chunk = "\n".join(lines[start - 1 : end])
        return chunk, start

    def write_text(self, path: str, content: str) -> Path:
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def list_dir(self, path: str = ".") -> list[str]:
        target = self.resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"path not found: {self.rel(target)}")
        if not target.is_dir():
            return [self.rel(target)]
        entries = []
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            suffix = "/" if child.is_dir() else ""
            entries.append(self.rel(child) + suffix)
        return entries
