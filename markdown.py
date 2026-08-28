from __future__ import annotations

import re

from wheel_agent import style

_FENCE = re.compile(r"```(\w*)\n?(.*?)```", re.S)
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_OL = re.compile(r"^(\d+)\.\s+(.*)$")


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
    for raw in text.splitlines():
        if raw.startswith("### "):
            lines.append(style.bold(_render_inline(raw[4:])))
        elif raw.startswith("## "):
            lines.append(style.bold(style.cyan(_render_inline(raw[3:]))))
        elif raw.startswith("# "):
            lines.append(style.bold(style.cyan(_render_inline(raw[2:]))))
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
    return "\n".join(lines)
