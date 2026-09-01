"""工具输出的行/字节级截断：保头或保尾、超限部分溢出到工作区日志文件，
并附提示行告诉模型完整输出在哪。所有读文件/命令输出的工具共用。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 默认预算：2000 行或 50KB，先到先截。单行最长 500 字符（grep 命中行常很长）。
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
GREP_MAX_LINE_LENGTH = 500


@dataclass
class TruncationResult:
    """截断结果 + 元信息：提示行要告诉模型显示的是第几行到第几行、总共多少行。"""

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
    """UTF-8 字节数（按字节限额时不能用字符数）。"""
    return len(text.encode("utf-8"))


def utf8_prefix(text: str, max_bytes: int) -> str:
    """按字节切前缀，落在多字节字符中间时丢弃不完整的尾巴。"""
    if max_bytes <= 0:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> str:
    """单行截断（grep 命中行用）：超长行截断并标注。"""
    if len(line) <= max_chars:
        return line
    return line[:max_chars] + "... [truncated]"


def truncate_head(
    content: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """保头部截断：适合看文件开头、日志开头。"""
    return _truncate(content, max_lines, max_bytes, tail=False)


def truncate_tail(
    content: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """保尾部截断：适合命令输出（结果通常在最后）。"""
    return _truncate(content, max_lines, max_bytes, tail=True)


def spill_output(workspace: str | Path, content: str) -> Path:
    """把完整输出存到 <工作区>/.wheel/outputs/，返回路径。

    截断后模型还能用 read 工具把完整内容拿回来。"""
    out_dir = Path(workspace) / ".wheel" / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{stamp}_{uuid.uuid4().hex[:8]}.log"
    path.write_text(content, encoding="utf-8")
    return path


def with_notice(result: TruncationResult, full_path: str | None = None) -> str:
    """把截断后的文本加上提示行（显示范围 + 完整输出路径）。"""
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
    """工具输出截断的总入口：截断、溢出保存、拼提示行，一步到位。

    keep_prefix：调用者之后会重新拼回的头部（如 "path: …" 行）。
    只截 payload，头部原样保留，行/字节预算全花在真实输出上。"""
    body = content
    if keep_prefix and content.startswith(keep_prefix):
        body = content[len(keep_prefix) :]
    result = truncate_tail(body, max_lines, max_bytes) if tail else truncate_head(body, max_lines, max_bytes)
    if not result.truncated:
        return content   # 没超限：原样返回，不存溢出文件
    spilled = spill_output(workspace, content)
    rel = _rel(workspace, spilled)
    result.spill_path = rel
    notice = with_notice(result, rel)
    return keep_prefix + notice if keep_prefix else notice


def _rel(workspace: str | Path, path: Path) -> str:
    """溢出文件相对工作区的路径（提示行里显示给模型看）。"""
    try:
        return str(path.resolve().relative_to(Path(workspace).resolve()))
    except ValueError:
        return str(path)


def _truncate(content: str, max_lines: int, max_bytes: int, *, tail: bool) -> TruncationResult:
    """核心截断：未超限原样返回；超限按行预算或字节预算截，
    字节预算先爆时保留的第一行可能是不完整的（last_line_partial 标记）。"""
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

    # tail：从后往前收（保留最后 max_lines 行）；否则从前往后。
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
        # 字节预算先爆：一行都还没收时，收下这个字节约定下的不完整前缀；
        # 已有内容则直接停。
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
