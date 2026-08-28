from __future__ import annotations

import atexit
import os
import re
import shutil
import sys
import threading
import unicodedata

_ANSI_RE = re.compile(r"\033\[[0-9;]*[A-Za-z]")


def enabled() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("WHEEL_COLOR", "").lower() in {"0", "false", "no"}:
        return False
    return sys.stdout.isatty()


def is_tty() -> bool:
    return sys.stdout.isatty()


def term_size() -> tuple[int, int]:
    """(rows, cols) from the tty ioctl, not the stale COLUMNS env var."""
    fds: list[int] = []
    for stream in (sys.stdout, sys.stdin, sys.__stdout__, sys.__stdin__):
        fileno = getattr(stream, "fileno", None)
        if not callable(fileno):
            continue
        try:
            fds.append(fileno())
        except Exception:
            continue
    for fd in fds:
        try:
            size = os.get_terminal_size(fd)
        except OSError:
            continue
        if size.lines > 0 and size.columns > 0:
            return max(1, size.lines), max(1, size.columns)
    size = shutil.get_terminal_size(fallback=(80, 24))
    return max(1, size.lines), max(1, size.columns)


_ACTIVE_FOOTER: Footer | None = None
OUTPUT_LOCK = threading.RLock()


def writeln(text: str = "") -> None:
    # Always CR+LF: raw mode (the line editor) does not translate \n.
    stream_write((text or "") + "\r\n")


def stream_write(text: str) -> None:
    """Write in the scroll region, then put the cursor back on pinned `>` if shown."""
    with OUTPUT_LOCK:
        footer = _ACTIVE_FOOTER
        pinned = footer is not None and footer.input_text is not None and is_tty()
        if pinned:
            sys.stdout.write("\0338")
        sys.stdout.write(text)
        sys.stdout.flush()
        if pinned:
            sys.stdout.write("\0337")
            footer._focus_input()
            sys.stdout.flush()


def crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def _wrap(code: str, text: str) -> str:
    if not enabled() or not text:
        return text
    # Inner resets would otherwise drop the outer style (bold(cyan(...))).
    inner = text.replace("\033[0m", f"\033[0m\033[{code}m")
    return f"\033[{code}m{inner}\033[0m"


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def cell_width(ch: str) -> int:
    """Terminal cells for one code point. Ambiguous (box drawing) is 2: CJK
    terminals render U+2500 as wide, so a cols-long ─ rule wraps and shoves
    later lines to the right (pi-tui counts conservative width to avoid this)."""
    if not ch or ch in "\n\r":
        return 0
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in {"W", "F", "A"} else 1


def display_width(text: str) -> int:
    return sum(cell_width(ch) for ch in strip_ansi(text))


def rule_line(cols: int) -> str:
    # ASCII hyphen is Na (narrow). U+2500 is ambiguous and wraps on zh locales.
    return "-" * max(0, cols)


def display_rows(text: str, cols: int | None = None) -> int:
    if not text:
        return 0
    cols = cols or term_size()[1]
    cols = max(1, cols)
    rows = 0
    for line in text.split("\n"):
        width = display_width(line)
        rows += 1 if width == 0 else (width - 1) // cols + 1
    return rows


def open_block_rows(body: str, cols: int | None = None) -> int:
    """Terminal rows used by `print(header)` plus a streamed body."""
    cols = cols or term_size()[1]
    return 1 + max(1, display_rows(body, cols))


def fit_display(text: str, cols: int) -> str:
    cols = max(0, cols)
    if display_width(text) <= cols:
        return text
    out: list[str] = []
    width = 0
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\033":
            m = _ANSI_RE.match(text, i)
            if m:
                out.append(m.group(0))
                i = m.end()
                continue
        extra = cell_width(text[i])
        if width + extra > cols:
            break
        out.append(text[i])
        width += extra
        i += 1
    return "".join(out)


def wrap_display(text: str, cols: int) -> list[str]:
    cols = max(1, cols)
    if not text:
        return [""]
    lines: list[str] = []
    rest = text
    while rest:
        piece = fit_display(rest, cols)
        if not piece:
            piece = rest[0]
        if not rest.startswith(piece):
            lines.append(rest)
            break
        lines.append(piece)
        rest = rest[len(piece):]
    return lines


def writeln_wrapped(text: str = "", cols: int | None = None) -> None:
    """Write lines wrapped to cols-1 so the terminal never auto-wraps (pi-tui)."""
    if cols is None:
        cols = max(1, term_size()[1] - 1)
    cols = max(1, cols)
    if not text:
        writeln("")
        return
    for line in text.split("\n"):
        for piece in wrap_display(line, cols):
            writeln(piece)


def replace_last_rows(row_count: int, new_text: str, *, reserved_bottom: int | None = None) -> None:
    """Erase the last `row_count` rows and write `new_text` in their place."""
    if reserved_bottom is None:
        reserved_bottom = Footer.HEIGHT
    payload = ""
    if not is_tty() or row_count <= 0:
        if new_text:
            payload = crlf(new_text if new_text.endswith("\n") else new_text + "\n")
        if payload:
            stream_write(payload)
        return
    _rows, _cols = term_size()
    limit = max(1, _rows - max(0, reserved_bottom) - 1)
    if row_count > limit:
        payload = "\r\n"
        if new_text:
            payload += crlf(new_text if new_text.endswith("\n") else new_text + "\n")
        stream_write(payload)
        return
    payload = "\r\033[2K" + "\033[1A\033[2K" * max(0, row_count - 1)
    if new_text:
        payload += crlf(new_text if new_text.endswith("\n") else new_text + "\n")
    stream_write(payload)


def bold(text: str) -> str:
    return _wrap("1", text)


def dim(text: str) -> str:
    return _wrap("2", text)


def italic(text: str) -> str:
    return _wrap("3", text)


def cyan(text: str) -> str:
    return _wrap("36", text)


def green(text: str) -> str:
    return _wrap("32", text)


def yellow(text: str) -> str:
    return _wrap("33", text)


def red(text: str) -> str:
    return _wrap("31", text)


def magenta(text: str) -> str:
    return _wrap("35", text)


def banner() -> str:
    art = [
        "  ╔════════════════════════╗",
        "  ║         WHEEL          ║",
        "  ║  minimal coding agent  ║",
        "  ╚════════════════════════╝",
    ]
    painted = [bold(cyan(line)) for line in art[:2]]
    painted.append(dim(art[2]))
    painted.append(bold(cyan(art[3])))
    return "\n".join(painted)


def prefix_block(label: str, body: str, paint) -> str:
    text = body if body else " "
    return frame(label, paint(text), paint)


def frame(label: str, rendered: str, paint=bold) -> str:
    """Wrap already-rendered text (e.g. markdown) without recoloring each line."""
    return "\n".join([dim("┌ ") + paint(bold(label)), rendered, dim("└")])


class Footer:
    """Pinned rows: optional plan, then rule, working directory, meter."""

    HEIGHT = 3

    def __init__(self) -> None:
        self.text = ""
        self.cwd = ""
        self.plan_lines: list[str] = []
        self._armed = False
        self._hooked = False
        self._pinned = 0
        self._size_armed: tuple[int, int] | None = None
        self._lock = threading.RLock()
        self._resized = threading.Event()
        self.input_text: str | None = None

    def height(self) -> int:
        return self._height_for(self._size()[0])

    def _input_rows(self) -> int:
        return 1 if self.input_text is not None else 0

    def _height_for(self, rows: int) -> int:
        input_h = self._input_rows()
        extra = len(self.plan_lines)
        max_extra = max(0, rows - self.HEIGHT - input_h - 2)
        return self.HEIGHT + input_h + min(extra, max_extra)

    def set_input(self, text: str | None, *, stream_row: int | None = None) -> None:
        """None hides the pinned `>` row; a string (even empty) shows it.

        On show, DECSC (the stream cursor) is seeded at `stream_row` — the row
        right after the user's task — so turn output continues there instead of
        jumping to the last scroll row. Showing the row grows the footer, which
        scrolls the region up, so the seed is lifted with the scroll.
        """
        with OUTPUT_LOCK:
            was = self.input_text
            showing = was is None and text is not None
            hiding = was is not None and text is None
            if hiding and is_tty():
                sys.stdout.write("\0338")
                sys.stdout.flush()
            self.input_text = text
            old_pinned = self._pinned or self.HEIGHT
            self.paint()
            if showing and is_tty() and self._armed:
                rows, _ = self._size()
                bottom = max(1, rows - self.height())
                grew = max(0, (self._pinned or self.HEIGHT) - old_pinned)
                row = stream_row - grew if stream_row is not None else None
                if row is None or not (1 <= row <= bottom):
                    row = bottom
                sys.stdout.write(f"\033[{row};1H\0337")
                self._focus_input()
                sys.stdout.flush()

    def _focus_input(self) -> None:
        if self.input_text is None or not is_tty():
            return
        rows, cols = self._size()
        usable = max(1, cols - 1)
        typed = fit_display(self.input_text.replace("\n", " "), max(0, usable - 2))
        col = min(max(1, cols), 1 + display_width("> ") + display_width(typed))
        sys.stdout.write(f"\033[{rows - 3};{col}H")

    def set_plan(self, lines: list[str] | None) -> None:
        self.plan_lines = [str(ln) for ln in (lines or [])]
        self.paint()

    def arm(self, *, reset: bool = False) -> None:
        """Reserve the last two rows. reset=True clears the screen and starts at the top."""
        if not is_tty():
            return
        with self._lock:
            self._arm_locked(reset=reset)

    def _arm_locked(self, *, reset: bool = False) -> None:
        rows, cols = self._size()
        if reset:
            # Drop DECSTBM first: CSI 2 J with a stale scroll region misses rows
            # and leaves wrapped slash-menu ghosts after a width change.
            sys.stdout.write("\033[r\033[2J\033[H")
        if rows < 5:
            self._armed = False
            self._pinned = 0
            self._size_armed = (rows, cols)
            sys.stdout.flush()
            return
        h = self._height_for(rows)
        old_h = self._pinned or self.HEIGHT
        if not reset and self._armed and h > old_h:
            n = h - old_h
            if self.input_text is None:
                sys.stdout.write("\0337")
                sys.stdout.write(f"\033[{n}S")
                sys.stdout.write("\0338")
            else:
                sys.stdout.write(f"\033[{n}S")
                # DECSC holds the stream cursor: lift it with the scroll so the
                # next stream write lands on the content, not the new footer.
                sys.stdout.write(f"\0338\033[{n}A\0337")
        elif not reset and self._armed and h < old_h:
            if self.input_text is None:
                sys.stdout.write("\0337")
                for r in range(rows - old_h + 1, rows - h + 1):
                    sys.stdout.write(f"\033[{r};1H\033[2K")
                sys.stdout.write("\0338")
            else:
                for r in range(rows - old_h + 1, rows - h + 1):
                    sys.stdout.write(f"\033[{r};1H\033[2K")
        bottom = rows - h
        if reset:
            sys.stdout.write(f"\033[1;{bottom}r\033[H")
        else:
            # DECSTBM homes the cursor. Save first and restore after so we stay
            # in the content region instead of jumping onto the pinned rows.
            # Skip DECSC when the busy `>` is up: that slot holds the stream cursor.
            if self.input_text is None:
                sys.stdout.write("\0337")
            sys.stdout.write(f"\033[1;{bottom}r")
            if self.input_text is None:
                sys.stdout.write("\0338")
        self._armed = True
        self._pinned = h
        self._size_armed = (rows, cols)
        global _ACTIVE_FOOTER
        _ACTIVE_FOOTER = self
        if not self._hooked:
            atexit.register(self.disarm)
            self._hooked = True
        sys.stdout.flush()

    def disarm(self) -> None:
        with self._lock:
            if not self._armed:
                return
            rows, _ = self._size()
            h = self._pinned or self.HEIGHT
            sys.stdout.write("\0337")
            for r in range(max(1, rows - h + 1), rows + 1):
                sys.stdout.write(f"\033[{r};1H\033[2K")
            sys.stdout.write("\033[?7h\033[r\0338")
            self._armed = False
            self._pinned = 0
            self._size_armed = None
            global _ACTIVE_FOOTER
            if _ACTIVE_FOOTER is self:
                _ACTIVE_FOOTER = None
                sys.stdout.flush()

    def set(self, text: str, *, cwd: str | None = None) -> None:
        self.text = text
        if cwd is not None:
            self.cwd = cwd
        self.paint()

    def notify_resize(self) -> None:
        self._resized.set()

    def consume_resize(self, *, reset: bool = False) -> bool:
        notified = self._resized.is_set()
        if notified:
            self._resized.clear()
        if not is_tty():
            return False
        rows, cols = self._size()
        if not notified and self._size_armed == (rows, cols):
            return False
        self.relayout(reset=reset)
        return True

    def relayout(self, *, reset: bool = False) -> None:
        if not is_tty():
            return
        with self._lock:
            rows, cols = self._size()
            changed = self._size_armed != (rows, cols)
            # reset=True wipes the conversation. Idle SIGWINCH only moves DECSTBM
            # and the footer so the terminal can reflow printed turns.
            self._arm_locked(reset=reset and changed)
            if rows >= 5 and (self.text or self._armed):
                self._paint_locked(rows, cols)

    def paint(self) -> None:
        if not is_tty() or (not self.text and not self._armed):
            return
        with OUTPUT_LOCK:
            with self._lock:
                rows, cols = self._size()
                if rows < 5:
                    return
                if not self._armed or (rows, cols) != self._size_armed or self._pinned != self._height_for(rows):
                    self._arm_locked()
                self._paint_locked(rows, cols)

    def _paint_locked(self, rows: int, cols: int) -> None:
        usable = max(1, cols - 1)
        h = self._height_for(rows)
        input_h = self._input_rows()
        extra = h - self.HEIGHT - input_h
        shown = self.plan_lines
        if extra <= 0:
            shown = []
        elif len(shown) > extra:
            shown = shown[: extra - 1] + ["…"]
        rule = rule_line(usable)
        cwd = fit_display(self.cwd, usable)
        body = fit_display(self.text, usable)
        plan = [fit_display(ln, usable) for ln in shown]
        if enabled():
            rule = dim(rule)
            cwd = dim(cwd)
            body = dim(body)
            painted = []
            for src, fitted in zip(shown, plan):
                if "[>]" in src:
                    painted.append(bold(cyan(fitted)))
                else:
                    painted.append(dim(fitted))
            plan = painted
        # DECAWM off: a miscounted glyph must not wrap the scroll region.
        # When `>` is pinned, DECSC holds the stream cursor — don't overwrite it.
        parts = ["\033[?7l"]
        if self.input_text is None:
            parts.append("\0337")
        start = rows - h + 1
        for i, ln in enumerate(plan):
            parts.append(f"\033[{start + i};1H\033[2K{ln}")
        typed = ""
        if self.input_text is not None:
            mark = "> "
            if enabled():
                mark = bold(cyan("> "))
            typed = fit_display(self.input_text.replace("\n", " "), max(0, usable - 2))
            parts.append(f"\033[{rows - 3};1H\033[2K{mark}{typed}")
        parts.append(f"\033[{rows - 2};1H\033[2K{rule}")
        parts.append(f"\033[{rows - 1};1H\033[2K{cwd}")
        parts.append(f"\033[{rows};1H\033[2K{body}")
        if self.input_text is None:
            parts.append("\0338")
        else:
            col = min(max(1, cols), 1 + display_width("> ") + display_width(typed))
            parts.append(f"\033[{rows - 3};{col}H")
        parts.append("\033[?7h")
        sys.stdout.write("".join(parts))
        sys.stdout.flush()

    def prompt_rule(self) -> str:
        cols = self._size()[1] if is_tty() else 40
        line = rule_line(max(1, cols - 1))
        return dim(line) if enabled() else line

    def _size(self) -> tuple[int, int]:
        return term_size()
