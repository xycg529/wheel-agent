"""终端 Markdown 渲染：把模型的 Markdown 回复转成带 ANSI 样式的终端文本。

支持围栏代码块、标题、引用、列表、链接/加粗/斜体/行内代码、GFM 表格。
表格按显示宽度对齐（中文字符按 2 宽算）。"""

from __future__ import annotations

import re

from wheel_agent.ui import style

# 各类 Markdown 语法的正则。
_FENCE = re.compile(r"```(\w*)\n?(.*?)```", re.S)
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_OL = re.compile(r"^(\d+)\.\s+(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
# GFM 分隔行：可选竖线，每段只有 : - :（可有空格）。
# 单破折号段是合法 GFM；像 "| - |" 这种数据行按规范是歧义的，
# 与参考 GFM 解析器一致当分隔行处理。
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _split_row(line: str) -> list[str]:
    """把一行管道表格拆成单元格（去空白）。"""
    return [cell.strip() for cell in _TABLE_ROW.match(line).group(1).split("|")]


def _is_table_row(line: str) -> bool:
    """是管道行但不是虚线分隔行（分隔行也匹配 _TABLE_ROW）。"""
    return bool(_TABLE_ROW.match(line)) and not _TABLE_SEP.match(line)


def render_markdown(text: str) -> str:
    """入口：按围栏代码块切段，块内走块级渲染，块间走代码块渲染。"""
    chunks: list[str] = []
    cursor = 0
    for match in _FENCE.finditer(text):
        chunks.append(_render_blocks(text[cursor : match.start()]))
        chunks.append(_render_fence(match.group(1), match.group(2)))
        cursor = match.end()
    chunks.append(_render_blocks(text[cursor:]))
    return "\n".join(chunk for chunk in chunks if chunk != "")


def _render_fence(lang: str, body: str) -> str:
    """渲染围栏代码块为带边框的暗色框（┌ │ └）。"""
    label = lang.strip() or "code"
    lines = body.rstrip("\n").splitlines() or [""]
    out = [style.dim(f"┌ {label}")]
    out.extend(style.dim("│ ") + line for line in lines)
    out.append(style.dim("└"))
    return "\n".join(out)


def _render_inline(text: str) -> str:
    """行内样式：链接（青色，只留文字）→加粗→行内代码→斜体。"""
    text = _LINK.sub(lambda m: style.cyan(m.group(1)), text)
    text = _BOLD.sub(lambda m: style.bold(m.group(1)), text)
    text = _CODE.sub(lambda m: style.yellow(m.group(1)), text)
    text = _ITALIC.sub(lambda m: style.italic(m.group(1)), text)
    return text


def _render_blocks(text: str) -> str:
    """块级渲染：逐行识别表格/标题/引用/列表/有序号/普通段。"""
    if not text:
        return ""
    lines: list[str] = []
    i = 0
    src = text.splitlines()
    while i < len(src):
        raw = src[i]
        # 表格：表头行 + 分隔行 + 数据行，全是管道分隔。
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
    """把管道表格渲染成带边框的网格，按显示宽度对齐。

    列宽用原始文本算（从不用样式后的文本——ANSI 转义会干扰宽度计算）；
    填充先按原始宽度算好再加样式，所以有没颜色对齐都不跑。
    分隔行在这里丢弃（而不是调用方），表中间混进的分隔行能优雅降级。

    边框用 ASCII：U+2500 框线字符在 CJK 终端按 2 宽、其他环境按 1 宽，
    一旦列宽计算遇上中文 locale，unicode 网格就错位。"""
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
    # 边框用 ASCII（见上面 docstring 说明）。
    rule = [style.dim("-" * (w + 2)) for w in widths]
    hborder = style.dim("+") + style.dim("+").join(rule) + style.dim("+")
    out = [hborder]
    for ridx, r in enumerate(parsed):
        cells = []
        for c, cell in enumerate(r):
            rendered = style.bold(_render_inline(cell)) if ridx == 0 else _render_inline(cell)
            # 行内标记（反引号、链接 URL）会让样式后的文本变短；
            # 按渲染后宽度对原始列宽补空格。
            pad = " " * max(0, widths[c] - style.display_width(rendered))
            cells.append(f" {rendered}{pad} ")
        out.append(style.dim("|") + style.dim("|").join(cells) + style.dim("|"))
        if ridx == 0:
            out.append(hborder)
    out.append(hborder)
    return "\n".join(out)
