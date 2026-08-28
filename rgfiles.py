from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".wheel_runs",
    ".wheel",
    ".pytest_cache",
    "node_modules",
    "build",
    "dist",
    "target",
    ".gradle",
    "generated",
}
IGNORE_GLOBS = (
    "!.git/**",
    "!.venv/**",
    "!.wheel/**",
    "!.wheel_runs/**",
    "!**/node_modules/**",
    "!**/build/**",
    "!**/dist/**",
    "!**/target/**",
    "!**/.gradle/**",
    "!**/__pycache__/**",
    "!**/.pytest_cache/**",
    "!**/generated/**",
)
DEFAULT_LIMIT = 200


def expand_braces(pattern: str) -> list[str]:
    """Expand one level of bash-style {a,b} so rg -g can see each alternative."""
    start = pattern.find("{")
    end = pattern.find("}", start + 1) if start >= 0 else -1
    if start < 0 or end < 0 or "," not in pattern[start + 1 : end]:
        return [pattern]
    inner = pattern[start + 1 : end]
    prefix, suffix = pattern[:start], pattern[end + 1 :]
    out: list[str] = []
    for part in inner.split(","):
        out.extend(expand_braces(prefix + part + suffix))
    return out or [pattern]


def rg_bin() -> str | None:
    return shutil.which("rg")


def glob_files(root: Path, pattern: str, limit: int = DEFAULT_LIMIT) -> list[Path]:
    rg = rg_bin()
    if rg:
        cmd = [rg, "--files", "--hidden", "--no-ignore"]
        for g in expand_braces(pattern):
            cmd.extend(["-g", g])
        for g in IGNORE_GLOBS:
            cmd.extend(["--glob", g])
        try:
            proc = subprocess.run(
                cmd,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return _glob_walk(root, pattern, limit)
        if proc.returncode not in {0, 1}:
            return _glob_walk(root, pattern, limit)
        hits: list[Path] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            hits.append((root / line).resolve())
            if len(hits) >= limit:
                break
        return hits
    return _glob_walk(root, pattern, limit)


def grep_files(
    root: Path,
    pattern: str,
    *,
    glob: str | None = None,
    limit: int = DEFAULT_LIMIT,
    max_line: int = 500,
) -> list[str]:
    rg = rg_bin()
    if rg:
        cmd = [rg, "-n", "--hidden", "--no-ignore", "--no-heading", "-e", pattern]
        if glob:
            for g in expand_braces(glob):
                cmd.extend(["-g", g])
        for g in IGNORE_GLOBS:
            cmd.extend(["--glob", g])
        cmd.append(str(root))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return _grep_walk(root, pattern, glob=glob, limit=limit, max_line=max_line)
        if proc.returncode not in {0, 1}:
            return _grep_walk(root, pattern, glob=glob, limit=limit, max_line=max_line)
        hits: list[str] = []
        for line in proc.stdout.splitlines():
            if len(line) > max_line + 80:
                line = line[: max_line + 80] + "…"
            hits.append(line)
            if len(hits) >= limit:
                hits.append("...[truncated]")
                break
        return hits
    return _grep_walk(root, pattern, glob=glob, limit=limit, max_line=max_line)


def _name_matches(name: str, rel: str, pattern: str) -> bool:
    rel_n = rel.replace("\\", "/")
    name_n = name.replace("\\", "/")
    for alt in expand_braces(pattern):
        alt = alt.replace("\\", "/")
        if alt.startswith("./"):
            alt = alt[2:]
        if _glob_match(rel_n, alt) or _glob_match(name_n, alt):
            return True
    return False


def _glob_match(text: str, pattern: str) -> bool:
    return _glob_re(pattern).fullmatch(text) is not None


@lru_cache(maxsize=256)
def _glob_re(pattern: str) -> re.Pattern[str]:
    """gitignore/rg-style glob: ** matches zero or more directories."""
    i, n = 0, len(pattern)
    out: list[str] = ["^"]
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i) and (i + 2 == n):
            out.append(".*" if out == ["^"] or out[-1] == "/" else "(?:/.*)?")
            i += 2
            continue
        c = pattern[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def _glob_walk(root: Path, pattern: str, limit: int) -> list[Path]:
    hits: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            rel = str(path.relative_to(root))
            if _name_matches(name, rel, pattern):
                hits.append(path)
                if len(hits) >= limit:
                    return hits
    return hits


def _grep_walk(
    root: Path,
    pattern: str,
    *,
    glob: str | None,
    limit: int,
    max_line: int,
) -> list[str]:
    compiled = re.compile(pattern)
    files: list[Path] = []
    if root.is_file():
        files = [root]
        base = root.parent
    else:
        base = root
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                rel = str(path.relative_to(root))
                if glob and not _name_matches(name, rel, glob):
                    continue
                files.append(path)
    hits: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, PermissionError, FileNotFoundError, OSError):
            continue
        try:
            rel = str(path.resolve().relative_to(base.resolve()))
        except ValueError:
            rel = str(path)
        for i, line in enumerate(text.splitlines(), start=1):
            if compiled.search(line):
                shown = line if len(line) <= max_line else line[:max_line] + "…"
                hits.append(f"{rel}:{i}:{shown}")
                if len(hits) >= limit:
                    hits.append("...[truncated]")
                    return hits
    return hits
