"""文件搜索：优先用系统 ripgrep（快、大仓库友好），
没有 rg 或它失败时退回纯 Python 的 os.walk 实现。glob 与 grep 都有。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

# 跳过的大目录（VCS/虚拟环境/构建产物），两套实现共用。
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
DEFAULT_LIMIT = 200   # 默认最多返回 200 个结果，防大仓库打爆上下文


def expand_braces(pattern: str) -> list[str]:
    """展开一层 bash 风格的 {a,b}，让 rg -g 能看到每个备选（rg 不展开花括号）。"""
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
    """系统 ripgrep 路径；没有返回 None。"""
    return shutil.which("rg")


def glob_files(root: Path, pattern: str, limit: int = DEFAULT_LIMIT) -> list[Path]:
    """按文件名模式找路径：rg --files -g <pattern>；rg 不可用/失败时走 _glob_walk。"""
    rg = rg_bin()
    if rg:
        cmd = [rg, "--files", "--hidden", "--no-ignore"]
        for g in expand_braces(pattern):
            cmd.extend(["-g", g])
        for g in IGNORE_GLOBS:   # --no-ignore 后手动重加排除项
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
            return _glob_walk(root, pattern, limit)   # rg 报错/超时：纯 Python 回退
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
    """搜文件内容：rg -n --no-heading；glob 参数过滤文件名。超限行截断，
    超 limit 条时追加 ...[truncated] 提示。"""
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
    """文件名或相对路径任一命中 pattern 就算匹配。"""
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
    """glob 全匹配（编译结果按 pattern 缓存）。"""
    return _glob_re(pattern).fullmatch(text) is not None


@lru_cache(maxsize=256)
def _glob_re(pattern: str) -> re.Pattern[str]:
    """gitignore/rg 风格 glob 转正则：** 匹配零或多个目录。"""
    i, n = 0, len(pattern)
    out: list[str] = ["^"]
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i) and (i + 2 == n):
            # 末尾的 **：开头时匹配任意深度，否则匹配可选的 / 后任意内容。
            out.append(".*" if out == ["^"] or out[-1] == "/" else "(?:/.*)?")
            i += 2
            continue
        c = pattern[i]
        if c == "*":
            out.append("[^/]*")   # 单星不跨目录
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def _glob_walk(root: Path, pattern: str, limit: int) -> list[Path]:
    """纯 Python 回退版 glob：跳过 SKIP_DIRS 和符号链接。

    符号链接跳过（与 rg --files 一致）：链接和目标会指向同一真实路径，
    下游 resolve() 后同一文件会以两个名字出现。"""
    hits: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [   # 原地剪枝：不进入跳过目录/符号链接目录
            d for d in dirnames if d not in SKIP_DIRS and not (Path(dirpath) / d).is_symlink()
        ]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
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
    """纯 Python 回退版 grep：root 是文件时只搜它；输出 path:行号:内容。"""
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
                # 单行超 max_line 截断（长行/压缩 JSON 常见）。
                shown = line if len(line) <= max_line else line[:max_line] + "…"
                hits.append(f"{rel}:{i}:{shown}")
                if len(hits) >= limit:
                    hits.append("...[truncated]")
                    return hits
    return hits
