from __future__ import annotations

import atexit
import os
import select
import sys
from pathlib import Path
from typing import Callable, Iterable

from wheel_agent import style
from wheel_agent.atfiles import at_token, replace_at_token
from wheel_agent.style import display_width, wrap_display

SLASH_CATALOG: tuple[tuple[str, str, str], ...] = (
    ("/help", "本说明", "/help"),
    ("/quit", "退出", "/quit"),
    ("/exit", "退出", "/exit"),
    ("/q", "退出", "/q"),
    ("/provider", "↑↓ 切换模型通道", "/provider [name]"),
    ("/effort", "↑↓ 当前模型的推理档位", "/effort [level]"),
    ("/think", "同 /effort", "/think [level]"),
    ("/replay", "↑↓ 时间线或重放", "/replay [run_id] [go]"),
    ("/replay session", "按 session 顺序重放全部 run", "/replay session [dir]"),
    ("/compact", "压缩当前会话历史", "/compact"),
    ("/undo", "撤销最近的 write/edit", "/undo [n]"),
    ("/undo-task", "回滚最近一个 task 的文件", "/undo-task"),
    ("/new", "新开会话", "/new"),
    ("/sessions", "列出本项目会话", "/sessions"),
    ("/resume", "恢复会话", "/resume [id]"),
    ("/plan", "打印当前 plan", "/plan"),
    ("/harness", "查看 continual harness", "/harness"),
    ("/refine", "从轨迹提取 prompt/memory", "/refine [--global] [text|rollback <id>]"),
    ("/refine auto", "每 N 回合后台抽取", "/refine auto [N|off]"),
    ("/jobs", "列出后台 bash", "/jobs"),
    ("/jobs kill", "↑↓ 杀掉后台作业", "/jobs kill [id]"),
    ("/tree", "会话树，↑↓ 回车跳转", "/tree [id]"),
    ("/graph", "当前路径的 turn/工具 DAG（文本）", "/graph"),
    ("/graph html", "写出 HTML，只打印路径和 http", "/graph html"),
    ("/fork", "同 /tree", "/fork [id]"),
    ("/follow", "停机后再投递", "/follow <text>"),
    ("/stop", "中止当前任务", "/stop"),
    ("/expand", "展开工具输出", "/expand r12"),
    ("/max-turns", "查看或设置 turn 上限", "/max-turns [n]"),
)

COMMANDS = tuple(cmd for cmd, _summary, _usage in SLASH_CATALOG) + (
    "help",
    "quit",
    "exit",
    "provider",
    "effort",
    "think",
    "replay",
    "compact",
    "undo",
    "undo-task",
    "new",
    "sessions",
    "resume",
    "plan",
    "tree",
    "graph",
    "fork",
    "follow",
    "stop",
    "expand",
    "replay session",
)


def slash_matches(prefix: str, words: Iterable[str] | None = None, *, limit: int = 12) -> list[str]:
    text = (prefix or "").strip()
    if not text.startswith("/"):
        return []
    pool = [cmd for cmd, _summary, _usage in SLASH_CATALOG] if words is None else list(words)
    seen: list[str] = []
    for word in pool:
        if not word.startswith(text) or word in seen:
            continue
        seen.append(word)
        if len(seen) >= limit:
            break
    return seen


def slash_accept(buf: str, selected: int = 0, words: Iterable[str] | None = None) -> str:
    matches = slash_matches(buf, words)
    if not matches:
        return buf
    return matches[max(0, min(selected, len(matches) - 1))]


def _pad(text: str, width: int) -> str:
    extra = width - style.display_width(text)
    if extra > 0:
        return text + " " * extra
    return style.fit_display(text, width)


def format_slash_menu(commands: list[str], selected: int = 0, cols: int = 80) -> list[str]:
    info = {cmd: (summary, usage) for cmd, summary, usage in SLASH_CATALOG}
    rows: list[tuple[str, str, str, str]] = []
    for i, cmd in enumerate(commands):
        summary, usage = info.get(cmd, ("", cmd))
        mark = ">" if i == selected else " "
        rows.append((f"  {mark} ", cmd, summary, usage))
    if not rows:
        return []
    cols = max(1, cols)
    prefix_w = 4
    gap = 2
    budget = max(0, cols - prefix_w)
    cmd_w = max(style.display_width(cmd) for _mark, cmd, _s, _u in rows)
    sum_w = max((style.display_width(s) for _m, _c, s, _u in rows), default=0)
    cmd_w = min(max(cmd_w, 8), 22)
    sum_w = min(max(sum_w, 4), 18)
    while cmd_w + gap + sum_w + gap + 8 > budget and sum_w > 4:
        sum_w -= 1
    while cmd_w + gap + sum_w + gap + 8 > budget and cmd_w > 8:
        cmd_w -= 1
    usage_w = max(0, budget - cmd_w - gap - sum_w - gap)
    lines: list[str] = []
    for mark, cmd, summary, usage in rows:
        line = mark + _pad(cmd, cmd_w)
        if sum_w:
            line += " " * gap + _pad(summary, sum_w)
        if usage_w:
            line += " " * gap + _pad(usage, usage_w)
        lines.append(style.fit_display(line, cols))
    return lines


def _fd_pending(fd: int, timeout: float = 0.0) -> bool:
    try:
        ready, _, _ = select.select([fd], [], [], timeout)
    except (InterruptedError, ValueError, OSError):
        return False
    return bool(ready)


def _read_byte(fd: int, timeout: float | None = None) -> bytes | None:
    if timeout is not None and not _fd_pending(fd, timeout):
        return None
    return os.read(fd, 1)


def decode_csi(params: str, final: str) -> str:
    arrows = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}
    if final in arrows:
        return arrows[final]
    if final == "~":
        return {
            "200": "paste_start",
            "201": "paste_end",
            "3": "delete",
            "1": "home",
            "4": "end",
            "7": "home",
            "8": "end",
        }.get(params, "esc")
    if final == "u":
        # Kitty keyboard protocol: \x1b[<code>;<mods>u (13=Enter, 27=Esc,
        # printable ASCII codes otherwise). Modified Enter must submit, not abort.
        code = params.split(";", 1)[0]
        try:
            key_code = int(code)
        except ValueError:
            return "esc"
        if key_code == 13:
            return "\r"
        if key_code == 27:
            return "esc"
        if 0x20 <= key_code <= 0x7E:
            return chr(key_code)
        return "esc"
    return "esc"


def enter_submits(*, pasting: bool, more_input: bool) -> bool:
    return not pasting and not more_input


def editor_visual(buf: str, cur: int, prompt_w: int, usable: int) -> tuple[list[str], int, int]:
    inner = max(1, usable - prompt_w)
    parts = buf.split("\n")
    remain = max(0, min(cur, len(buf)))
    line_i = 0
    col = 0
    for i, part in enumerate(parts):
        if remain <= len(part):
            line_i = i
            col = remain
            break
        remain -= len(part) + 1
    else:
        line_i = max(0, len(parts) - 1)
        col = len(parts[line_i]) if parts else 0
    rows: list[str] = []
    cur_row = 0
    cur_col = prompt_w
    for i, part in enumerate(parts):
        pieces = wrap_display(part, inner) or [""]
        if i == line_i:
            acc = 0
            for j, piece in enumerate(pieces):
                plen = len(piece)
                if acc + plen >= col or j == len(pieces) - 1:
                    cur_row = len(rows) + j
                    cur_col = prompt_w + display_width(piece[: max(0, col - acc)])
                    break
                acc += plen
        rows.extend(pieces)
    if not rows:
        rows = [""]
    return rows, cur_row, cur_col


def cursor_vert(buf: str, cur: int, delta: int) -> int | None:
    parts = buf.split("\n")
    if len(parts) < 2:
        return None
    remain = max(0, min(cur, len(buf)))
    line_i = 0
    col = 0
    for i, part in enumerate(parts):
        if remain <= len(part):
            line_i = i
            col = remain
            break
        remain -= len(part) + 1
    else:
        line_i = len(parts) - 1
        col = len(parts[-1]) if parts else 0
    dest = line_i + delta
    if dest < 0 or dest >= len(parts):
        return None
    dest_col = min(col, len(parts[dest]))
    return sum(len(part) + 1 for part in parts[:dest]) + dest_col


def query_cursor_row(fd: int) -> int | None:
    """Ask the terminal for the current cursor row (\\033[6n); None on timeout."""
    with style.OUTPUT_LOCK:
        sys.stdout.write("\033[6n")
        sys.stdout.flush()
    buf = b""
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], 0.05)
        except (InterruptedError, ValueError, OSError):
            return None
        if not ready:
            return None
        chunk = os.read(fd, 1)
        if not chunk:
            return None
        buf += chunk
        if not buf.endswith(b"R"):
            continue
        if not buf.startswith(b"\x1b[") or b";" not in buf:
            return None
        try:
            return int(buf[2 : buf.index(b";")])
        except ValueError:
            return None


def _read_key(fd: int, timeout: float | None = None) -> str | None:
    first = _read_byte(fd, timeout)
    if first is None:
        return None
    if first == b"":
        return "\x04"
    if first[0] & 0x80:
        extra = 1
        if first[0] & 0xE0 == 0xC0:
            extra = 1
        elif first[0] & 0xF0 == 0xE0:
            extra = 2
        elif first[0] & 0xF8 == 0xF0:
            extra = 3
        rest = b""
        while len(rest) < extra:
            chunk = _read_byte(fd)
            if not chunk:
                break
            rest += chunk
        return (first + rest).decode("utf-8", "replace")
    ch = first.decode("latin1")
    if ch != "\x1b":
        return ch
    nxt = _read_byte(fd, timeout=0.03)
    if not nxt:
        return "esc"
    nxt_byte = nxt[0]
    if nxt_byte == 0x1B:
        return "esc"
    if nxt_byte & 0x80:
        # ESC + UTF-8 lead byte: Alt+non-ASCII. Decode the full character.
        extra = 1
        if nxt_byte & 0xE0 == 0xC0:
            extra = 1
        elif nxt_byte & 0xF0 == 0xE0:
            extra = 2
        elif nxt_byte & 0xF8 == 0xF0:
            extra = 3
        rest = b""
        while len(rest) < extra:
            chunk = _read_byte(fd)
            if not chunk:
                break
            rest += chunk
        return (nxt + rest).decode("utf-8", "replace")
    if nxt_byte != 0x5B:  # "["
        # ESC + a plain byte is a modified key, not a standalone ESC: many
        # terminals send Shift+Enter as \x1b\r. Returning "esc" here aborted
        # the running task on every Shift+Enter and desynced the UI.
        return nxt.decode("latin1")
    params = ""
    while True:
        chunk = _read_byte(fd, timeout=0.05)
        if not chunk:
            return "esc"
        code = chunk.decode("latin1", "replace")
        if 0x40 <= ord(code) <= 0x7E:
            return decode_csi(params, code)
        params += code
        if len(params) > 24:
            return "esc"


def is_busy_abort_key(key: str) -> bool:
    return key in {"\x03", "esc"}


def enter_busy_tty(fd: int):
    """Char-at-a-time, no echo, keep ONLCR so `print()`/`\n` still return to column 0.

    setraw also clears OPOST, which is what staircase-indented say/think frames.
    ISIG is cleared so Ctrl+C arrives as a byte instead of SIGINT.
    """
    import termios
    import tty

    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    attrs = termios.tcgetattr(fd)
    attrs[3] &= ~termios.ISIG
    termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
    return old


class BusyPrompt:
    """Pinned `>` above the footer while a run streams. Keys never echo into say."""

    def __init__(self, footer: style.Footer) -> None:
        self.footer = footer
        self.buf = ""
        self.pasting = False

    def show(self, stream_row: int | None = None) -> None:
        self.footer.set_input(self.buf, stream_row=stream_row)

    def hide(self) -> None:
        self.footer.set_input(None)

    def feed(self, key: str) -> str | None:
        if key == "paste_start":
            self.pasting = True
            return None
        if key == "paste_end":
            self.pasting = False
            return None
        if key in {"\r", "\n"}:
            if self.pasting:
                self.buf += "\n"
                self.footer.set_input(self.buf)
                return None
            line = self.buf
            self.buf = ""
            self.footer.set_input("")
            return line
        if key in {"\x7f", "\x08"}:
            if self.buf:
                self.buf = self.buf[:-1]
                self.footer.set_input(self.buf)
            return None
        if key == "\x15":
            self.buf = ""
            self.footer.set_input("")
            return None
        if len(key) == 1 and key.isprintable():
            self.buf += key
            self.footer.set_input(self.buf)
        return None


class LineEditor:
    """Stdlib readline: history + Tab completion. Clipboard stays with the terminal."""

    def __init__(
        self,
        words: Iterable[str] | None = None,
        history_path: Path | None = None,
        on_idle: Callable[[], bool] | None = None,
        on_paint: Callable[[], None] | None = None,
        at_files: Callable[[str], list[str]] | None = None,
        reserved_bottom: Callable[[], int] | int | None = None,
    ):
        self.words = list(words or COMMANDS)
        self.history_path = history_path or (Path.home() / ".wheel_history")
        self.on_idle = on_idle
        self.on_paint = on_paint
        self.at_files = at_files
        self.reserved_bottom = reserved_bottom
        self.last_cursor_row: int | None = None
        self._matches: list[str] = []
        self.available = False
        try:
            import readline
        except ImportError:
            return
        self.readline = readline
        self.available = True
        readline.set_completer(self.complete)
        readline.set_completer_delims(" \t\n")
        try:
            readline.parse_and_bind("tab: complete")
        except Exception:
            readline.parse_and_bind("bind ^I rl_complete")
        try:
            readline.parse_and_bind("set enable-bracketed-paste on")
        except Exception:
            pass
        if self.history_path.exists():
            try:
                readline.read_history_file(self.history_path)
            except OSError:
                pass
        readline.set_history_length(500)
        atexit.register(self._save)

    def set_words(self, words: Iterable[str]) -> None:
        self.words = list(words)

    def _palette(self, buf: str, cur: int, *, pasting: bool = False) -> list[str]:
        if pasting:
            return []
        if buf.startswith("/") and "\n" not in buf:
            return slash_matches(buf)
        if not self.at_files:
            return []
        token = at_token(buf, cur)
        if not token:
            return []
        return self.at_files(token)

    def complete(self, text: str, state: int) -> str | None:
        if state == 0:
            if text.startswith("/"):
                self._matches = slash_matches(text, self.words, limit=20)
            elif text.startswith("@") and self.at_files:
                self._matches = self.at_files(text)[:20]
            elif not text:
                self._matches = [w for w in self.words if w.startswith("/")]
            else:
                self._matches = [w for w in self.words if w.startswith(text)]
        return self._matches[state] if state < len(self._matches) else None

    def prompt(self) -> str:
        if not style.enabled():
            return "> "
        # RL_PROMPT_START_IGNORE / END_IGNORE so cursor math ignores ANSI.
        return "\001\033[1;36m\002> \001\033[0m\002"

    def read(self) -> str:
        sys.stdout.flush()
        if sys.stdin.isatty() and sys.stdout.isatty():
            try:
                return self._read_tty()
            except OSError:
                pass
        return input(self.prompt())

    def _visible_prompt(self) -> str:
        return "> " if not style.enabled() else "\033[1;36m> \033[0m"

    def _footer_rows(self) -> int:
        spec = self.reserved_bottom
        if callable(spec):
            try:
                return max(0, int(spec()))
            except (TypeError, ValueError):
                return style.Footer.HEIGHT
        if spec is not None:
            return max(0, int(spec))
        return style.Footer.HEIGHT

    def _read_tty(self) -> str:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        buf = ""
        cur = 0
        selected = 0
        palette_rows = 0
        pasting = False
        unread: list[str] = []
        history = self._history_lines()
        hist_i = len(history)
        original: str | None = None
        try:
            tty.setraw(fd)
            sys.stdout.write("\033[?2004h")
            sys.stdout.flush()
            self._prompt_row = None
            palette_rows = self._draw_line(buf, [], 0, 0, cur=cur)
            while True:
                if unread:
                    key = unread.pop(0)
                else:
                    key = _read_key(fd, timeout=0.12)
                if key is None:
                    if self.on_idle and self.on_idle():
                        matches = self._palette(buf, cur, pasting=pasting)
                        if selected >= len(matches):
                            selected = 0
                        palette_rows = self._draw_line(
                            buf, matches, selected, palette_rows, wipe=True, cur=cur
                        )
                    continue
                if key == "\x03":
                    self._commit_line(palette_rows, buf)
                    raise KeyboardInterrupt
                if key == "\x04" and not buf:
                    self._commit_line(palette_rows, buf)
                    raise EOFError
                if key == "paste_start":
                    pasting = True
                    continue
                if key == "paste_end":
                    pasting = False
                    matches = self._palette(buf, cur)
                    palette_rows = self._draw_line(buf, matches, selected, palette_rows, cur=cur)
                    continue
                if key in {"\r", "\n"}:
                    if key == "\r":
                        nxt = unread.pop(0) if unread else _read_key(fd, timeout=0)
                        if nxt is not None and nxt != "\n":
                            unread.insert(0, nxt)
                    more = bool(unread) or _fd_pending(fd, 0.0 if pasting else 0.02)
                    if not enter_submits(pasting=pasting, more_input=more):
                        buf = buf[:cur] + "\n" + buf[cur:]
                        cur += 1
                        selected = 0
                        if pasting:
                            continue
                    else:
                        pal = self._palette(buf, cur)
                        if pal:
                            pick = pal[min(selected, len(pal) - 1)]
                            if buf.startswith("/") and "\n" not in buf:
                                buf = pick
                            else:
                                buf, cur = replace_at_token(buf, cur, pick)
                        self._commit_line(palette_rows, buf)
                        if buf.strip() and self.available:
                            try:
                                self.readline.add_history(buf)
                            except Exception:
                                pass
                        return buf
                elif pasting and len(key) == 1:
                    buf = buf[:cur] + key + buf[cur:]
                    cur += 1
                    selected = 0
                    continue
                elif key in {"\x7f", "\x08"}:
                    if cur > 0:
                        buf = buf[: cur - 1] + buf[cur:]
                        cur -= 1
                    selected = 0
                elif key in {"left", "\x02"}:
                    cur = max(0, cur - 1)
                elif key in {"right", "\x06"}:
                    cur = min(len(buf), cur + 1)
                elif key in {"home", "\x01"}:
                    cur = 0
                elif key in {"end", "\x05"}:
                    cur = len(buf)
                elif key == "\t":
                    matches = self._palette(buf, cur)
                    if matches:
                        pick = matches[min(selected, len(matches) - 1)]
                        if buf.startswith("/") and "\n" not in buf:
                            buf = pick
                            cur = len(buf)
                        else:
                            buf, cur = replace_at_token(buf, cur, pick)
                        selected = 0
                elif key == "\x15":
                    buf = buf[cur:]
                    cur = 0
                    selected = 0
                elif key == "up":
                    matches = self._palette(buf, cur)
                    moved = cursor_vert(buf, cur, -1)
                    if matches:
                        selected = (selected - 1) % len(matches)
                    elif moved is not None:
                        cur = moved
                    elif history:
                        if hist_i == len(history):
                            original = buf  # readline: remember the in-progress line when entering history
                        hist_i = max(0, hist_i - 1)
                        buf = history[hist_i]
                        cur = len(buf)
                        selected = 0
                elif key == "down":
                    matches = self._palette(buf, cur)
                    moved = cursor_vert(buf, cur, 1)
                    if matches:
                        selected = (selected + 1) % len(matches)
                    elif moved is not None:
                        cur = moved
                    elif history:
                        if hist_i < len(history):
                            hist_i += 1
                            if hist_i >= len(history):
                                # Bottom of history: restore the line that was being
                                # edited before history navigation (readline behavior);
                                # previously this cleared the buffer and lost it.
                                buf = original if original is not None else ""
                                original = None
                            else:
                                buf = history[hist_i]
                            cur = len(buf)
                            selected = 0
                elif len(key) == 1 and key.isprintable():
                    buf = buf[:cur] + key + buf[cur:]
                    cur += 1
                    selected = 0
                    hist_i = len(history)
                matches = self._palette(buf, cur, pasting=pasting)
                if selected >= len(matches):
                    selected = 0
                palette_rows = self._draw_line(buf, matches, selected, palette_rows, cur=cur)
        finally:
            sys.stdout.write("\033[?2004l")
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _history_lines(self) -> list[str]:
        if self.available:
            try:
                n = int(self.readline.get_current_history_length() or 0)
                return [self.readline.get_history_item(i) or "" for i in range(1, n + 1)]
            except Exception:
                pass
        if not self.history_path.exists():
            return []
        try:
            return [ln for ln in self.history_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            return []

    def _cursor_row(self) -> int | None:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return None
        return query_cursor_row(sys.stdin.fileno())

    def _commit_line(self, extra_rows: int, buf: str = "") -> None:
        row = getattr(self, "_prompt_row", 0) or 1
        rows, cols = style.term_size()
        bottom = max(1, rows - self._footer_rows())
        usable = max(1, cols - 1)
        prompt = self._visible_prompt()
        prompt_w = style.display_width(style.strip_ansi(prompt))
        visual, _, _ = editor_visual(buf, len(buf), prompt_w, usable)
        last = min(bottom, row + max(extra_rows, max(0, len(visual) - 1)))
        for r in range(row, last + 1):
            sys.stdout.write(f"\033[{r};1H\033[2K")
        indent = " " * prompt_w
        sys.stdout.write(f"\033[{row};1H" + prompt + (visual[0] if visual else ""))
        for line in visual[1:]:
            sys.stdout.write("\r\n" + indent + line)
        sys.stdout.write("\r\n")
        # Absolute row the cursor now sits on (right below the committed line),
        # clamped to the scroll region bottom. Used to seed the stream cursor
        # for the next task without a racy mid-task terminal query.
        self.last_cursor_row = min(row + len(visual), bottom)
        if self.on_paint:
            self.on_paint()
        sys.stdout.flush()

    def _draw_line(
        self,
        buf: str,
        matches: list[str],
        selected: int,
        old_rows: int,
        *,
        wipe: bool = False,
        cur: int | None = None,
    ) -> int:
        rows, cols = style.term_size()
        rows, cols = max(5, rows), max(1, cols)
        bottom = max(1, rows - self._footer_rows())
        # Last column wraps; a wrap on the DECSTBM bottom row scrolls a second menu.
        usable = max(1, cols - 1)
        prompt_row = getattr(self, "_prompt_row", None)
        if wipe:
            found = self._cursor_row()
            if found is not None:
                prompt_row = found
            elif prompt_row is None:
                prompt_row = 1
        elif prompt_row is None:
            prompt_row = self._cursor_row() or 1
        prompt_row = min(max(1, prompt_row), bottom)
        prompt = self._visible_prompt()
        prompt_w = style.display_width(style.strip_ansi(prompt))
        if cur is None:
            cur = len(buf)
        cur = max(0, min(cur, len(buf)))
        visual, cur_row, cur_col = editor_visual(buf, cur, prompt_w, usable)
        body_rows = len(visual)
        menu_want = min(len(matches), max(0, bottom - 1))
        body_keep = min(body_rows, max(1, bottom - menu_want))
        if body_rows > body_keep:
            skip = body_rows - body_keep
            visual = visual[skip:]
            cur_row = max(0, cur_row - skip)
            body_rows = len(visual)
        # Need room below the prompt: scroll the DECSTBM region up instead of
        # CUPing the prompt over the conversation.
        last = prompt_row + body_rows + menu_want - 1
        if last > bottom and prompt_row > 1:
            deficit = min(last - bottom, prompt_row - 1)
            if deficit:
                sys.stdout.write(f"\033[{deficit}S")
                prompt_row -= deficit
        prompt_row = min(max(1, prompt_row), max(1, bottom - body_rows + 1))
        max_menu = max(0, bottom - prompt_row - max(0, body_rows - 1))
        matches = matches[:max_menu]
        menu = format_slash_menu(matches, selected, usable) if matches else []
        new_rows = len(menu)
        extra = max(0, body_rows - 1) + new_rows
        clear_from = prompt_row
        clear_to = bottom if wipe else min(bottom, prompt_row + max(old_rows, extra))
        indent = " " * prompt_w
        for r in range(clear_from, clear_to + 1):
            sys.stdout.write(f"\033[{r};1H\033[2K")
        sys.stdout.write(f"\033[{prompt_row};1H" + prompt + visual[0])
        for i, line in enumerate(visual[1:], start=1):
            sys.stdout.write(f"\033[{prompt_row + i};1H\033[2K" + indent + line)
        menu_row = prompt_row + body_rows
        for i, line in enumerate(menu):
            painted = style.cyan(line) if line.lstrip().startswith(">") and style.enabled() else style.dim(line)
            sys.stdout.write(f"\033[{menu_row + i};1H\033[2K" + painted)
        cup_row = min(bottom, prompt_row + cur_row)
        cup_col = min(cols, max(1, cur_col + 1))
        sys.stdout.write(f"\033[{cup_row};{cup_col}H")
        if self.on_paint:
            self.on_paint()
        sys.stdout.flush()
        self._prompt_row = prompt_row
        return extra

    def _save(self) -> None:
        if not self.available:
            return
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            self.readline.write_history_file(self.history_path)
        except OSError:
            pass


def pick_list(options: list[str], selected: int = 0) -> int | None:
    """Arrow-key picker. Returns index, or None if cancelled."""
    if not options:
        return None
    selected = max(0, min(selected, len(options) - 1))
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return selected
    import termios
    import tty

    n = len(options)
    cols = max(1, style.term_size()[1] - 1)

    def paint(*, first: bool = False) -> None:
        if not first:
            sys.stdout.write(f"\033[{n}A")
        for i, opt in enumerate(options):
            mark = ">" if i == selected else " "
            raw = f"  {mark} {opt}"
            painted = style.cyan(raw) if i == selected and style.enabled() else style.dim(raw)
            sys.stdout.write("\r\033[2K" + style.fit_display(painted, cols) + "\r\n")
        sys.stdout.flush()

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    paint(first=True)
    try:
        tty.setraw(fd)
        while True:
            try:
                key = _read_key(fd)
            except OSError:
                # tty/pipe closed mid-pick (window closed, pipe broken): cancel
                # instead of traceback — dispatch only catches KeyboardInterrupt.
                return None
            if key in {None, "\x03", "\x04", "q", "\x1b", "esc"}:
                return None
            if key in {"\r", "\n"}:
                return selected
            if key == "up":
                selected = (selected - 1) % n
            elif key == "down":
                selected = (selected + 1) % n
            else:
                continue
            paint()
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except OSError:
            pass  # fd already gone; the cancel above is the useful outcome
        style.writeln("")


def completion_words(
    providers: Iterable[str],
    skills: Iterable[str] | None = None,
    effort_levels: Iterable[str] | None = None,
) -> list[str]:
    words = list(COMMANDS)
    for name in providers:
        words.append(f"/provider {name}")
        words.append(f"provider {name}")
    for level in effort_levels or ():
        words.append(f"/effort {level}")
        words.append(f"/think {level}")
    words.extend(["/refine auto", "/refine auto off", "/jobs", "/jobs kill"])
    for skill in skills or ():
        words.append(f"/skill:{skill}")
    return words
