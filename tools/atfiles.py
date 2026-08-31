"""@文件引用：REPL 里输入 @ 时补全工作区路径。

at_span 定位光标处的 @token，list_at_files 给出候选路径。"""

from __future__ import annotations

from pathlib import Path

from wheel_agent.tools.rgfiles import glob_files


def at_span(buf: str, cur: int) -> tuple[int, int] | None:
    """返回光标处 @token 的 [start, end)，不是 @token 则 None。"""
    n = len(buf)
    cur = max(0, min(cur, n))
    # 光标在词尾后一个位置时，回退一格再定位整词。
    i = cur
    if i > 0 and (i == n or buf[i] in " \t\n") and buf[i - 1] not in " \t\n":
        i -= 1
    start = i
    while start > 0 and buf[start - 1] not in " \t\n":
        start -= 1
    end = start
    while end < n and buf[end] not in " \t\n":
        end += 1
    if start < n and buf[start] == "@":
        return start, end
    return None


def at_token(buf: str, cur: int) -> str | None:
    """光标处 @token 的完整文本（含 @），否则 None。"""
    span = at_span(buf, cur)
    if span is None:
        return None
    return buf[span[0] : span[1]]


def replace_at_token(buf: str, cur: int, replacement: str) -> tuple[str, int]:
    """把光标处 @token 替换为 replacement，返回（新文本, 新光标位置）。"""
    span = at_span(buf, cur)
    if span is None:
        return buf, cur
    new = buf[: span[0]] + replacement + buf[span[1] :]
    return new, span[0] + len(replacement)


def list_at_files(root: str | Path, token: str, limit: int = 12) -> list[str]:
    """给 @token 返回候选路径（最多 limit 个），名称前缀命中优先于路径子串命中。"""
    prefix = token[1:] if token.startswith("@") else token
    prefix = prefix.replace("\\", "/").lower()
    if prefix.startswith("./"):
        prefix = prefix[2:]
    root = Path(root)
    hits: list[tuple[int, str]] = []
    seen: set[str] = set()
    root_real = root.resolve()
    for path in glob_files(root, "*", limit=200):
        try:
            real = path.resolve()
            rel = real.relative_to(root_real).as_posix()
        except (OSError, RuntimeError, ValueError):
            continue
        if str(real) in seen:
            continue
        seen.add(str(real))
        low = rel.lower()
        name = Path(rel).name.lower()
        # 排序键 rank：0 = 名字/路径前缀命中，1 = 路径子串命中。
        if prefix == "":
            rank = 0
        elif name.startswith(prefix) or low.startswith(prefix):
            rank = 0
        elif prefix in low:
            rank = 1
        else:
            continue
        hits.append((rank, rel))
        if len(hits) >= 80:
            break
    hits.sort()
    return ["@" + rel for _rank, rel in hits[:limit]]
