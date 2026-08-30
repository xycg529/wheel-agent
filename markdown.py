from __future__ import annotations

import re

from wheel_agent import style

_FENCE = re.compile(r"```(\w*)\n?(.*?)```", re.S)
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_OL = re.compile(r"^(\d+)\.\s+(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
# GFM separator: optional pipes, every segment is only : - : (spaces allowed).
# Single-dash segments are valid GFM; a data row like "| - |" is ambiguous per
# spec and treated as a separator, same as reference GFM parsers.
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in _TABLE_ROW.match(line).group(1).split("|")]


def _is_table_row(line: str) -> bool:
    """A pipe row that is NOT the dashed separator (the separator also matches _TABLE_ROW)."""
    return bool(_TABLE_ROW.match(line)) and not _TABLE_SEP.match(line)


def render_markdown(text: str) -> str:
    chunks: list[str] = []
    cursor = 0
    for match in _FENCE.finditer(text):
        chunks.append(_render_blocks(text[cursor : match.start()]))
        chunks.append(_render_fence(match.group(1), match.group(2)))
        cursor = match.end()
    chunks.append(_render_blocks(text[cursor:]))
    return "\n".join(chunk for chunk in chunks if chunk != "")


def _render_fence(lang: str, body: str) -> str:
    label = lang.strip() or "code"
    lines = body.rstrip("\n").splitlines() or [""]
    out = [style.dim(f"┌ {label}")]
    out.extend(style.dim("│ ") + line for line in lines)
    out.append(style.dim("└"))
    return "\n".join(out)


def _render_inline(text: str) -> str:
    text = _LINK.sub(lambda m: style.cyan(m.group(1)), text)
    text = _BOLD.sub(lambda m: style.bold(m.group(1)), text)
    text = _CODE.sub(lambda m: style.yellow(m.group(1)), text)
    text = _ITALIC.sub(lambda m: style.italic(m.group(1)), text)
    return text


def _render_blocks(text: str) -> str:
    if not text:
        return ""
    lines: list[str] = []
    i = 0
    src = text.splitlines()
    while i < len(src):
        raw = src[i]
        # Tables: header row + separator row + body rows, all pipe-delimited.
        if _is_table_row(raw) and i + 1 < len(src) and _TABLE_SEP.match(src[i + 1]):
            j = i + 2
            while j < len(src) and _is_table_row(src[j]):
                j += 1
            lines.append(_render_table(src[i:j]))
            i = j
            continue
        if raw.startswith("### "):
            lines.append(style.bold(_render_inline(raw[4:])))
        elif raw.startswith("## ") or raw.startswith("# "):
            body = _render_inline(raw[raw.index(" ") + 1 :])
            lines.append(style.bold(style.cyan(body)))
        elif raw.startswith("> "):
            lines.append(style.dim("│ ") + style.italic(_render_inline(raw[2:])))
        elif raw.startswith("- ") or raw.startswith("* "):
            lines.append(style.dim("• ") + _render_inline(raw[2:]))
        else:
            numbered = _OL.match(raw)
            if numbered:
                lines.append(style.dim(f"{numbered.group(1)}. ") + _render_inline(numbered.group(2)))
            else:
                lines.append(_render_inline(raw))
        i += 1
    return "\n".join(lines)


def _render_table(rows: list[str]) -> str:
    """Render pipe-table rows as a bordered grid, aligned by display width.

    Column widths are computed from the raw cell text (never from styled
    text, whose ANSI escapes would skew width math); padding is applied to
    the raw width before styling, so alignment holds with colors on or off.
    Separator rows are dropped here rather than by the caller, so a stray
    separator mid-table degrades gracefully instead of rendering as data.
    """
    parsed = [_split_row(row) for row in rows if _is_table_row(row)]
    if not parsed:
        return "\n".join(_render_inline(row) for row in rows)
    ncols = max(len(r) for r in parsed)
    for r in parsed:
        r.extend([""] * (ncols - len(r)))
    widths = [0] * ncols
    for r in parsed:
        for c, cell in enumerate(r):
            widths[c] = max(widths[c], style.display_width(cell))
    # ASCII borders (like style.rule_line): U+2500 box chars are East-Asian
    # ambiguous — counted 2-wide on CJK terminals, 1-wide elsewhere — so a
    # unicode grid misaligns the moment column math meets a zh locale.
    rule = [style.dim("-" * (w + 2)) for w in widths]
    hborder = style.dim("+") + style.dim("+").join(rule) + style.dim("+")
    out = [hborder]
    for ridx, r in enumerate(parsed):
        cells = []
        for c, cell in enumerate(r):
            rendered = style.bold(_render_inline(cell)) if ridx == 0 else _render_inline(cell)
            # Inline markup (backticks, link URLs) shrinks text after styling;
            # pad by the RENDERED width against the raw-derived column width.
            pad = " " * max(0, widths[c] - style.display_width(rendered))
            cells.append(f" {rendered}{pad} ")
        out.append(style.dim("|") + style.dim("|").join(cells) + style.dim("|"))
        if ridx == 0:
            out.append(hborder)
    out.append(hborder)
    return "\n".join(out)
