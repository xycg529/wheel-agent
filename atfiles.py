from __future__ import annotations

from pathlib import Path

from wheel_agent.rgfiles import glob_files


def at_span(buf: str, cur: int) -> tuple[int, int] | None:
    """[start, end) of the @token at the cursor, or None."""
    n = len(buf)
    cur = max(0, min(cur, n))
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
    span = at_span(buf, cur)
    if span is None:
        return None
    return buf[span[0] : span[1]]


def replace_at_token(buf: str, cur: int, replacement: str) -> tuple[str, int]:
    span = at_span(buf, cur)
    if span is None:
        return buf, cur
    new = buf[: span[0]] + replacement + buf[span[1] :]
    return new, span[0] + len(replacement)


def list_at_files(root: str | Path, token: str, limit: int = 12) -> list[str]:
    prefix = token[1:] if token.startswith("@") else token
    prefix = prefix.replace("\\", "/").lower()
    if prefix.startswith("./"):
        prefix = prefix[2:]
    root = Path(root)
    hits: list[tuple[int, str]] = []
    for path in glob_files(root, "*", limit=200):
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        low = rel.lower()
        name = Path(rel).name.lower()
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
