"""终端样式与页脚：ANSI 颜色、显示宽度计算（CJK 感知）、
滚动区内写入、以及固定在底部的页脚（计划/分隔线/目录/计量）。"""

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
    """是否启用颜色：NO_COLOR/WHEEL_COLOR 关闭，且 stdout 是 TTY。"""
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("WHEEL_COLOR", "").lower() in {"0", "false", "no"}:
        return False
    return sys.stdout.isatty()


def is_tty() -> bool:
    """stdout 是否是终端。"""
    return sys.stdout.isatty()


def term_size() -> tuple[int, int]:
    """(rows, cols)，从 tty ioctl 读，不信可能过期的 COLUMNS 环境变量。

    试多个 fd：pty/harness 环境下 stdin 和 stdout 可能在不同设备上
    （或其中一个被捕获）；第一个报出合理尺寸的就用。"""
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


# 当前激活的页脚（stream_write 需要知道是否要保护固定输入行）。
_ACTIVE_FOOTER: Footer | None = None
# 保护并发写 stdout（页脚绘制 vs 流式输出）。
OUTPUT_LOCK = threading.RLock()


def writeln(text: str = "") -> None:
    # 永远用 CR+LF：raw 模式（行编辑器）不会翻译 \n。
    stream_write((text or "") + "\r\n")


def stream_write(text: str) -> None:
    """在滚动区内写入；若固定输入行 `>` 在显示，写完把光标放回它上面。"""
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
    """统一换行为 CRLF（raw 模式下 \n 不会自动变成 \r\n）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def _wrap(code: str, text: str) -> str:
    """用 ANSI 码包一层样式；不启用颜色时原样返回。"""
    if not enabled() or not text:
        return text
    # 内层的重置码会把外层样式丢掉（bold(cyan(...))）；
    # 把内层 0m 替换成“重置+重设本码”，保住嵌套样式。
    inner = text.replace("\033[0m", f"\033[0m\033[{code}m")
    return f"\033[{code}m{inner}\033[0m"


def strip_ansi(text: str) -> str:
    """去掉所有 ANSI 转义，得到纯文本。"""
    return _ANSI_RE.sub("", text)


def cell_width(ch: str) -> int:
    """一个码点占的终端格数。歧义宽（制表符等）算 2：CJK 终端把
    U+2500 渲染成宽字符，一条 cols 长的 ─ 会换行把后面的行挤到右边
    （pi-tui 用保守宽度避免这个）。"""
    if not ch or ch in "\n\r":
        return 0
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in {"W", "F", "A"} else 1


def display_width(text: str) -> int:
    """文本的显示宽度（先去 ANSI，逐码点累加）。"""
    return sum(cell_width(ch) for ch in strip_ansi(text))


def rule_line(cols: int) -> str:
    # 用 ASCII 短横线（Na 窄）：U+2500 是歧义宽，中文 locale 下会换行。
    return "-" * max(0, cols)


def display_rows(text: str, cols: int | None = None) -> int:
    """文本在 cols 宽度下占多少终端行（含自动换行）。"""
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
    """print(header) 加一段流式 body 共占的终端行数。"""
    cols = cols or term_size()[1]
    return 1 + max(1, display_rows(body, cols))


def fit_display(text: str, cols: int) -> str:
    """把文本截到 cols 宽度内（保留 ANSI，不截半截转义序列）。"""
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
                out.append(m.group(0))   # 整段转义序列原样保留
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
    """按显示宽度把文本折行（CJK 安全，不截半截码点）。"""
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
    """按 cols-1 折行后写入，让终端自己永不自动换行（pi-tui）。"""
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
    """擦掉最后 row_count 行，在原位写入 new_text。"""
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
        # 要擦的行比可用行还多：不逐行上移了，直接换行重写。
        payload = "\r\n"
        if new_text:
            payload += crlf(new_text if new_text.endswith("\n") else new_text + "\n")
        stream_write(payload)
        return
    payload = "\r\033[2K" + "\033[1A\033[2K" * max(0, row_count - 1)   # 回车清当前行，再逐行上移清行
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


def _clear_rows(first: int, last: int) -> None:
    for r in range(max(1, first), last + 1):
        sys.stdout.write(f"\033[{r};1H\033[2K")


class Footer:
    """固定行：可选的计划，然后分隔线、工作目录、计量。"""

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
        # 固定输入行 `>`：非 None 时页脚多一行显示已敲的文本（busy 时）。
        self.input_text: str | None = None

    def height(self) -> int:
        """当前页脚总高度（含计划行与输入行，受终端行数限制）。"""
        return self._height_for(self._size()[0])

    def _input_rows(self) -> int:
        """固定输入行占的高度（0 或 1）。"""
        return 1 if self.input_text is not None else 0

    def _height_for(self, rows: int) -> int:
        """给定终端行数，算页脚能多高：计划行在剩余空间里截断。"""
        input_h = self._input_rows()
        extra = len(self.plan_lines)
        max_extra = max(0, rows - self.HEIGHT - input_h - 2)
        return self.HEIGHT + input_h + min(extra, max_extra)

    def set_input(self, text: str | None, *, stream_row: int | None = None) -> None:
        """None 隐藏固定输入行 `>`；字符串（哪怕空）则显示。

        显示时在 stream_row（用户任务行下一行）种下 DECSC（流光标），
        让回合输出从那里继续，而不是跳到最后一个滚动行。
        显示行会让页脚变高、滚动区上移，所以种子也随滚动上移。"""
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
        """把光标放到固定输入行 `>` 的末尾（已输入文本之后）。"""
        if self.input_text is None or not is_tty():
            return
        rows, cols = self._size()
        usable = max(1, cols - 1)
        typed = fit_display(self.input_text.replace("\n", " "), max(0, usable - 2))
        col = min(max(1, cols), 1 + display_width("> ") + display_width(typed))
        sys.stdout.write(f"\033[{rows - 3};{col}H")

    def set_plan(self, lines: list[str] | None) -> None:
        """更新页脚里的计划行并重绘。"""
        self.plan_lines = [str(ln) for ln in (lines or [])]
        self.paint()

    def arm(self, *, reset: bool = False) -> None:
        """预留最后几行给页脚。reset=True 清屏并从顶部开始。"""
        if not is_tty():
            return
        with self._lock:
            self._arm_locked(reset=reset)

    def _arm_locked(self, *, reset: bool = False) -> None:
        rows, cols = self._size()
        if reset:
            # 先丢 DECSTBM：带过期滚动区的 CSI 2 J 会漏掉行，
            # 宽度变化后留下斜杠菜单的残影。
            sys.stdout.write("\033[r\033[2J\033[H")
        if rows < 5:
            # 终端太矮放不下页脚：不预留。
            self._armed = False
            self._pinned = 0
            self._size_armed = (rows, cols)
            sys.stdout.flush()
            return
        h = self._height_for(rows)
        old_h = self._pinned or self.HEIGHT
        if not reset and self._armed and h > old_h:
            # 页脚变高：把内容上滚 n 行。
            n = h - old_h
            if self.input_text is None:
                sys.stdout.write("\0337")
                sys.stdout.write(f"\033[{n}S")
                sys.stdout.write("\0338")
            else:
                sys.stdout.write(f"\033[{n}S")
                # DECSC 存着流光标：随滚动上移，让下一次流写入落在内容上
                # 而不是新页脚上。
                sys.stdout.write(f"\0338\033[{n}A\0337")
        elif not reset and self._armed and h < old_h:
            # 页脚变矮：清出多出来的行。
            if self.input_text is None:
                sys.stdout.write("\0337")
                _clear_rows(rows - old_h + 1, rows - h + 1)
                sys.stdout.write("\0338")
            else:
                _clear_rows(rows - old_h + 1, rows - h + 1)
        bottom = rows - h
        if reset:
            sys.stdout.write(f"\033[1;{bottom}r\033[H")
        else:
            # DECSTBM 会把光标送到滚动区底部。先存（DECSC）后恢复（DECRC），
            # 让光标留在内容区而不是跳到固定行上。
            # busy `>` 在显示时跳过 DECSC：那个槽位存的是流光标。
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
        """解除页脚预留：清固定行、恢复默认滚动区。"""
        with self._lock:
            if not self._armed:
                return
            rows, _ = self._size()
            h = self._pinned or self.HEIGHT
            sys.stdout.write("\0337")
            _clear_rows(rows - h + 1, rows)
            sys.stdout.write("\033[?7h\033[r\0338")
            self._armed = False
            self._pinned = 0
            self._size_armed = None
            global _ACTIVE_FOOTER
            if _ACTIVE_FOOTER is self:
                _ACTIVE_FOOTER = None
                sys.stdout.flush()

    def set(self, text: str, *, cwd: str | None = None) -> None:
        """更新页脚主文本（计量行）与目录，重绘。"""
        self.text = text
        if cwd is not None:
            self.cwd = cwd
        self.paint()

    def notify_resize(self) -> None:
        """标记终端尺寸变了（SIGWINCH 处理设）。"""
        self._resized.set()

    def consume_resize(self, *, reset: bool = False) -> bool:
        """消费 resize 标记；尺寸确实变了才重排。返回是否重排了。"""
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
            # reset=True 会清空对话。空闲时的 SIGWINCH 只挪 DECSTBM 和页脚，
            # 让终端能重新折已打印的回合。
            self._arm_locked(reset=reset and changed)
            if rows >= 5 and (self.text or self._armed):
                self._paint_locked(rows, cols)

    def paint(self) -> None:
        """重绘整个页脚（加锁，尺寸变了先重新 arm）。"""
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
            shown = shown[: extra - 1] + ["…"]   # 放不下时截断加省略号
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
                    painted.append(bold(cyan(fitted)))   # 进行中的计划步骤高亮
                else:
                    painted.append(dim(fitted))
            plan = painted
        # 关自动换行（DECAWM）：数错的字符不能把滚动区换行。
        # `>` 固定显示时 DECSC 存着流光标——不要覆盖它。
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
        """输入提示前的分隔线。"""
        cols = self._size()[1] if is_tty() else 40
        line = rule_line(max(1, cols - 1))
        return dim(line) if enabled() else line

    def _size(self) -> tuple[int, int]:
        """当前终端尺寸。"""
        return term_size()
