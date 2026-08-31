"""工作区：所有文件访问都经它收口，保证路径解析、越界拦截（沙箱）和
读/写/列目录的统一行为。read/write 都带行级 offset/limit 与编码容错。"""

from __future__ import annotations

from pathlib import Path


class Workspace:
    """把 agent 的文件活动限制在 root 之内；任何解析后跳出 root 的路径都拒绝。"""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str | Path) -> Path:
        """把相对/绝对路径解析到 root 内；解析后越界则 PermissionError（防 ../../ 逃逸）。"""
        raw = Path(path)
        candidate = raw.resolve() if raw.is_absolute() else (self.root / raw).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError(f"path escapes workspace: {path}") from exc
        return candidate

    def rel(self, path: Path) -> str:
        """root 相对路径（事件/日志里展示用）。"""
        return str(path.relative_to(self.root))

    def read_text(self, path: str, offset: int = 1, limit: int | None = None) -> tuple[str, int]:
        """读文件（1-based offset、limit 行），返回（片段, 实际起始行号）。"""
        target = self.resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"file not found: {self.rel(target)}")
        if not target.is_file():
            raise IsADirectoryError(f"not a file: {self.rel(target)}")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, offset)   # 防 0/负数 offset
        end = len(lines) if limit is None else min(len(lines), start - 1 + max(limit, 0))
        chunk = "\n".join(lines[start - 1 : end])
        return chunk, start

    def write_text(self, path: str, content: str) -> Path:
        """写文件（自动建父目录），返回解析后的绝对路径。"""
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def list_dir(self, path: str = ".") -> list[str]:
        """列一个目录（目录在前、名字小写排序），返回 root 相对路径（目录带 / 后缀）。"""
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
