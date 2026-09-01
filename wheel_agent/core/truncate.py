from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
GREP_MAX_LINE_LENGTH = 500


@dataclass
class TruncationResult:
    text: str
    truncated: bool
    truncated_by: str | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    start_line: int
    end_line: int
    last_line_partial: bool = False
    spill_path: str | None = None


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def utf8_prefix(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> str:
    if len(line) <= max_chars:
        return line
    return line[:max_chars] + "... [truncated]"


def truncate_head(
    content: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    return _truncate(content, max_lines, max_bytes, tail=False)


def truncate_tail(
    content: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    return _truncate(content, max_lines, max_bytes, tail=True)


def spill_output(workspace: str | Path, content: str) -> Path:
    out_dir = Path(workspace) / ".wheel" / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{stamp}_{uuid.uuid4().hex[:8]}.log"
    path.write_text(content, encoding="utf-8")
    return path


def with_notice(result: TruncationResult, full_path: str | None = None) -> str:
    if not result.truncated:
        return result.text
    path = full_path or result.spill_path or "(unsaved)"
    extra = " last line truncated." if result.last_line_partial else ""
    notice = (
        f"[Showing lines {result.start_line}-{result.end_line} of {result.total_lines}. "
        f"Full output: {path}]{extra}"
    )
    body = result.text.rstrip("\n")
    return f"{body}\n\n{notice}" if body else notice


def apply(
    content: str,
    workspace: str | Path,
    *,
    tail: bool,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep_prefix: str = "",
) -> str:
    # keep_prefix: a header the caller re-attaches afterwards (e.g. the
    # "path: …" line). Truncate the payload only, so the line/byte budget is
    # spent on real output and the header survives verbatim.
    body = content
    if keep_prefix and content.startswith(keep_prefix):
        body = content[len(keep_prefix) :]
    result = truncate_tail(body, max_lines, max_bytes) if tail else truncate_head(body, max_lines, max_bytes)
    if not result.truncated:
        return content
    spilled = spill_output(workspace, content)
    rel = _rel(workspace, spilled)
    result.spill_path = rel
    notice = with_notice(result, rel)
    return keep_prefix + notice if keep_prefix else notice


def _rel(workspace: str | Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path(workspace).resolve()))
    except ValueError:
        return str(path)


def _truncate(content: str, max_lines: int, max_bytes: int, *, tail: bool) -> TruncationResult:
    total_bytes = utf8_len(content)
    lines = content.split("\n")
    total_lines = len(lines)
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            text=content,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            start_line=1,
            end_line=total_lines,
        )

    kept: list[str] = []
    used = 0
    truncated_by: str | None = None
    last_partial = False
    first_idx = 0
    last_idx = -1

    order = range(total_lines - 1, -1, -1) if tail else range(total_lines)
    for idx in order:
        if len(kept) >= max_lines:
            truncated_by = "lines"
            break
        line = lines[idx]
        extra = utf8_len(line) + (1 if kept else 0)
        if used + extra <= max_bytes:
            if tail:
                kept.insert(0, line)
            else:
                kept.append(line)
            used += extra
            if last_idx < 0:
                first_idx = last_idx = idx
            elif tail:
                first_idx = idx
            else:
                last_idx = idx
            continue
        if not kept:
            prefix = utf8_prefix(line, max_bytes)
            if prefix:
                kept.append(prefix)
                first_idx = last_idx = idx
                last_partial = prefix != line
            truncated_by = "bytes"
        else:
            truncated_by = "bytes"
        break
    else:
        truncated_by = "lines" if total_lines > max_lines else "bytes"

    text = "\n".join(kept)
    return TruncationResult(
        text=text,
        truncated=True,
        truncated_by=truncated_by or "bytes",
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(kept),
        output_bytes=utf8_len(text),
        start_line=first_idx + 1,
        end_line=(last_idx + 1) if last_idx >= 0 else 0,
        last_line_partial=last_partial,
    )
