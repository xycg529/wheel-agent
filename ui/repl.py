"""交互 REPL 的输入层：斜杠命令目录与补全菜单、
带历史/Tab 补全/@文件补全的 TTY 行编辑器（stdlib readline 不可用时的自绘版）、
busy 期间的固定输入行、方向键选择器。"""

from __future__ import annotations

import atexit
import collections
import os
import select
import sys
import threading
from pathlib import Path
from typing import Callable, Iterable

from wheel_agent.ui import style
from wheel_agent.tools.atfiles import at_token, replace_at_token
from wheel_agent.ui.style import display_width, wrap_display

# 斜杠命令目录：(命令, 一句话说明, 用法)——/help 和 Tab 菜单的数据源。
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

# 仅 / 开头的命令（裸词不再是命令，只作为任务文本）。
COMMANDS = tuple(cmd for cmd, _summary, _usage in SLASH_CATALOG)


def slash_matches(prefix: str, words: Iterable[str] | None = None, *, limit: int = 12) -> list[str]:
    """前缀匹配斜杠命令（限 limit 个）；words 可传入自定义词表（含 /skill: 等）。"""
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


def _pad(text: str, width: int) -> str:
    """右填充到 width 显示宽度；超宽时截断。"""
    extra = width - style.display_width(text)
    if extra > 0:
        return text + " " * extra
    return style.fit_display(text, width)


def format_slash_menu(commands: list[str], selected: int = 0, cols: int = 80) -> list[str]:
    """把候选命令排成三列菜单（命令/说明/用法），窄屏时自动缩列宽。"""
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
        sum_w -= 1   # 先缩说明列，再缩命令列，用法列吃剩余
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
    """fd 在 timeout 内是否有数据可读（select）。"""
    try:
        ready, _, _ = select.select([fd], [], [], timeout)
    except (InterruptedError, ValueError, OSError):
        return False
    return bool(ready)


# 等 DSR（光标位置查询）报告期间消耗掉的、但不是报告的字节
# ——即查询中途到达的用户输入。暂存在这里让按键读取者拿回，而不是丢掉
# （pty harness 下报告永远不来，整个查询窗口都可能吃掉一整行输入）。
# 存 int（bytes 迭代出来是 int）；pop 时重新包成 1 字节的 bytes。
_INPUT_STASH: "collections.deque[int]" = collections.deque()
_STASH_LOCK = threading.Lock()


def _stash_input(data: bytes) -> None:
    """把查询中途吃掉的输入字节暂存起来。"""
    if data:
        with _STASH_LOCK:
            _INPUT_STASH.extend(data)


def _pop_stashed() -> bytes | None:
    """取回一个暂存字节（没有则 None）。"""
    with _STASH_LOCK:
        if not _INPUT_STASH:
            return None
        return bytes([_INPUT_STASH.popleft()])


def _read_byte(fd: int, timeout: float | None = None) -> bytes | None:
    """读一个字节：优先返回暂存输入；超时未就绪返回 None。"""
    stashed = _pop_stashed()
    if stashed is not None:
        return stashed
    if timeout is not None and not _fd_pending(fd, timeout):
        return None
    return os.read(fd, 1)


def _utf8_len(lead: int) -> int:
    """UTF-8 引导字节还需要多少个续字节。"""
    if lead & 0xF0 == 0xE0:
        return 2
    if lead & 0xF8 == 0xF0:
        return 3
    return 1


def decode_csi(params: str, final: str) -> str:
    """把 CSI 序列解成按键名（方向键/home/end/delete/粘贴标记等）。"""
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
        # Kitty 键盘协议：\x1b[<code>;<mods>u（13=Enter，27=Esc，
        # 其他为可打印 ASCII 码）。带修饰的 Enter 必须提交而不是中止。
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
    """Enter 是否提交：粘贴中或后面还有字节（Shift+Enter 的 \n）都不提交。"""
    return not pasting and not more_input


def editor_visual(buf: str, cur: int, prompt_w: int, usable: int) -> tuple[list[str], int, int]:
    """把缓冲区折成视觉行，并算出光标在哪个视觉行/列（多行编辑的显示基础）。"""
    inner = max(1, usable - prompt_w)
    parts = buf.split("\n")
    line_i, col = _cursor_pos(buf, cur)
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


def _cursor_pos(buf: str, cur: int) -> tuple[int, int]:
    """光标偏移 cur 在 buf 里的 (行号, 列号)。"""
    parts = buf.split("\n")
    remain = max(0, min(cur, len(buf)))
    for i, part in enumerate(parts):
        if remain <= len(part):
            return i, remain
        remain -= len(part) + 1
    return len(parts) - 1, len(parts[-1])


def cursor_vert(buf: str, cur: int, delta: int) -> int | None:
    """上下键跨行移动：返回目标行同列的偏移（列越界时贴边）；单行返回 None。"""
    if "\n" not in buf:
        return None
    line_i, col = _cursor_pos(buf, cur)
    parts = buf.split("\n")
    dest = line_i + delta
    if dest < 0 or dest >= len(parts):
        return None
    dest_col = min(col, len(parts[dest]))
    return sum(len(part) + 1 for part in parts[:dest]) + dest_col


def query_cursor_row(fd: int) -> int | None:
    """向终端询问当前光标行（DSR 查询）；超时返回 None。

    报告和键入输入共用同一个 fd，所以等待读循环只能消耗字节来找报告。
    消耗的字节若不是报告本身，就暂存（经 _INPUT_STASH）给按键读取者，
    而不是丢掉——否则输入在途时发出的查询会吃掉用户的一行
    （真终端毫秒级响应；pty 永不响应，整个窗口都致命）。
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    try:
        if select.select([fd], [], [], 0.0)[0]:
            return None  # 输入已在等待——留给读取者
    except (InterruptedError, ValueError, OSError):
        return None
    with _STASH_LOCK:
        if _INPUT_STASH:
            return None  # 有暂存输入——跳过查询
    with style.OUTPUT_LOCK:
        sys.stdout.write("\033[6n")
        sys.stdout.flush()
    buf = b""
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], 0.05)
        except (InterruptedError, ValueError, OSError):
            break
        if not ready:
            break
        chunk = os.read(fd, 1)
        if not chunk:
            break
        buf += chunk
        if buf.startswith(b"\x1b[") and b";" in buf and buf.endswith(b"R"):
            break
    if buf.startswith(b"\x1b["):
        end = buf.find(b"R")
        head, leftover = buf[: end + 1], buf[end + 1 :]
        try:
            row = int(head[2 : head.index(b";")])
        except (ValueError, IndexError):
            row, leftover = None, buf  # 其实不是报告——全是输入
        _stash_input(leftover)
        return row
    _stash_input(buf)
    return None


def _read_key(fd: int, timeout: float | None = None) -> str | None:
    """读一个逻辑按键（处理 UTF-8、ESC/CSI 序列、Kitty 协议），超时返回 None。"""
    first = _read_byte(fd, timeout)
    if first is None:
        return None
    if first == b"":
        return "\x04"
    if first[0] & 0x80:
        rest = b""
        while len(rest) < _utf8_len(first[0]):
            chunk = _read_byte(fd)
            if not chunk:
                break
            rest += chunk
        return (first + rest).decode("utf-8", "replace")
    ch = first.decode("latin1")
    if ch != "\x1b":
        return ch
    # ESC 消歧：单独 ESC 会单独到达；方向键/CSI 序列的下一字节
    # 几毫秒内就跟来，所以用 30ms 窗口区分两者。
    nxt = _read_byte(fd, timeout=0.03)
    if not nxt:
        return "esc"
    nxt_byte = nxt[0]
    if nxt_byte == 0x1B:
        return "esc"
    if nxt_byte & 0x80:
        # ESC + UTF-8 引导字节：Alt+非 ASCII。解码完整字符。
        rest = b""
        while len(rest) < _utf8_len(nxt_byte):
            chunk = _read_byte(fd)
            if not chunk:
                break
            rest += chunk
        return (nxt + rest).decode("utf-8", "replace")
    if nxt_byte != 0x5B:  # "["
        # ESC + 一个普通字节是带修饰的键，不是单独 ESC：很多终端把
        # Shift+Enter 发成 \x1b\r。这里返回 "esc" 会让每次 Shift+Enter
        # 都中止运行中的任务并让 UI 失同步。
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
    """busy 时算中止键的（Ctrl+C / Esc）。"""
    return key in {"\x03", "esc"}


def enter_busy_tty(fd: int):
    """逐字符、不回显，保留 ONLCR 让 print()/\n 仍回到行首。

    setraw 还会清掉 OPOST，正是它导致 say/think 框的楼梯式缩进。
    清 ISIG 让 Ctrl+C 作为字节到达而不是 SIGINT。"""
    import termios
    import tty

    tty.setcbreak(fd)
    attrs = termios.tcgetattr(fd)
    attrs[3] &= ~termios.ISIG
    termios.tcsetattr(fd, termios.TCSADRAIN, attrs)


class BusyPrompt:
    """运行时固定在页脚上方的 `>`；按键永远不回显进 say。"""

    def __init__(self, footer: style.Footer) -> None:
        self.footer = footer
        self.buf = ""
        self.pasting = False

    def show(self, stream_row: int | None = None) -> None:
        self.footer.set_input(self.buf, stream_row=stream_row)

    def hide(self) -> None:
        self.footer.set_input(None)

    def feed(self, key: str) -> str | None:
        """喂一个按键；返回非 None 表示提交了完整一行。"""
        if key == "paste_start":
            self.pasting = True
            return None
        if key == "paste_end":
            self.pasting = False
            return None
        if key in {"\r", "\n"}:
            if self.pasting:
                self.buf += "\n"   # 粘贴中的 Enter 是换行编辑
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
            self.buf = ""   # Ctrl+U：清空
            self.footer.set_input("")
            return None
        if len(key) == 1 and key.isprintable():
            self.buf += key
            self.footer.set_input(self.buf)
        return None


class LineEditor:
    """标准库 readline：历史 + Tab 补全。剪贴板留给终端自己处理。

    TTY 下用自绘行编辑器（raw 模式）；非 TTY 退化为 input()。"""

    def __init__(
        self,
        words: Iterable[str] | None = None,
        history_path: Path | None = None,
        on_idle: Callable[[], bool] | None = None,
        on_paint: Callable[[], None] | None = None,
        at_files: Callable[[str], list[str]] | None = None,
        reserved_bottom: Callable[[], int] | int | None = None,
    ):
        self.words = list(words or COMMANDS)   # 补全词表（含 /provider x 等变体）
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
            readline.parse_and_bind("bind ^I rl_complete")   # 老版 libedit 的绑法
        try:
            readline.parse_and_bind("set enable-bracketed-paste on")   # 括号粘贴
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
        """当前光标位置可用的补全列表（斜杠命令或 @文件）。"""
        if pasting:
            return []
        if buf.startswith("/") and "\n" not in buf:
            # words（经 set_words 设置）带 SLASH_CATALOG 没有的 /skill:name 和
            # /provider x 变体——合并起来，让自绘编辑器也能 Tab 补全它们。
            return slash_matches(buf, self.words) or slash_matches(buf)
        if not self.at_files:
            return []
        token = at_token(buf, cur)
        if not token:
            return []
        return self.at_files(token)

    def complete(self, text: str, state: int) -> str | None:
        """readline 补全器：/ 开头补命令，@ 开头补文件。"""
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
        # RL_PROMPT_START_IGNORE / END_IGNORE 让 readline 的光标计算忽略 ANSI。
        return "\001\033[1;36m\002> \001\033[0m\002"

    def read(self) -> str:
        """读一行：真 TTY 用自绘编辑器，否则退化为 input()。"""
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
        """页脚占用的行数（可回调或常量）。"""
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
        """raw 模式自绘行编辑主循环：处理按键、补全菜单、历史、提交。"""
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
            sys.stdout.write("\033[?2004h")   # 开括号粘贴
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
                        selected, palette_rows = self._refresh_palette(
                            buf, cur, selected, palette_rows, pasting, wipe=True
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
                    # Shift+Enter（和一些终端的 Enter）发 \r\n：\n 可能在
                    # \r 后几毫秒才到，所以用 20ms 窗口偷看一眼；如果还有字节，
                    # 这是换行编辑而不是提交。真正的 Enter 是单独的 \r。
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
                                self.readline.add_history(buf)   # 提交后入历史
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
                            buf = pick      # 命令：整行替换
                            cur = len(buf)
                        else:
                            buf, cur = replace_at_token(buf, cur, pick)   # @token：只替换 token
                        selected = 0
                elif key == "\x15":
                    buf = buf[cur:]   # Ctrl+U：删到行首
                    cur = 0
                    selected = 0
                elif key in {"up", "down"}:
                    delta = -1 if key == "up" else 1
                    matches = self._palette(buf, cur)
                    moved = cursor_vert(buf, cur, delta)
                    if matches:
                        selected = (selected + delta) % len(matches)   # 有补全菜单：在菜单里移动
                    elif moved is not None:
                        cur = moved                                    # 多行：跨行移动
                    elif history:
                        buf, hist_i, original = self._hist_move(buf, hist_i, original, history, delta)
                        cur = len(buf)
                        selected = 0
                elif len(key) == 1 and key.isprintable():
                    buf = buf[:cur] + key + buf[cur:]
                    cur += 1
                    selected = 0
                    hist_i = len(history)
                selected, palette_rows = self._refresh_palette(buf, cur, selected, palette_rows, pasting)
        finally:
            sys.stdout.write("\033[?2004l")   # 关括号粘贴
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _refresh_palette(
        self,
        buf: str,
        cur: int,
        selected: int,
        palette_rows: int,
        pasting: bool,
        *,
        wipe: bool = False,
    ) -> tuple[int, int]:
        """重画补全菜单（必要时先清掉旧行）；返回 (selected, 占用行数)。"""
        matches = self._palette(buf, cur, pasting=pasting)
        if selected >= len(matches):
            selected = 0
        return selected, self._draw_line(buf, matches, selected, palette_rows, wipe=wipe, cur=cur)

    def _hist_move(
        self, buf: str, hist_i: int, original: str | None, history: list[str], delta: int
    ) -> tuple[str, int, str | None]:
        """历史导航一步；返回 (buf, hist_i, original)。"""
        if delta < 0:
            if hist_i == len(history):
                original = buf  # readline 行为：进入历史时记住正在编辑的行
            hist_i = max(0, hist_i - 1)
            return history[hist_i], hist_i, original
        if hist_i < len(history):
            hist_i += 1
            if hist_i >= len(history):
                # 历史底部：恢复进入历史导航前正在编辑的那行
                #（readline 行为）；以前这里会清掉缓冲区把它弄丢。
                buf = original if original is not None else ""
                original = None
            else:
                buf = history[hist_i]
        return buf, hist_i, original

    def _history_lines(self) -> list[str]:
        """历史行列表：优先 readline 内存历史，否则读历史文件。"""
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
        # DSR (\033[6n) 只对真终端有效；管道或 pty 捕获永远不会回答
        #（而且泄漏的查询可能溜进测试 harness 的输入里），所以非 TTY 返回 None。
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return None
        return query_cursor_row(sys.stdin.fileno())

    def _commit_line(self, extra_rows: int, buf: str = "") -> None:
        """提交一行：擦掉编辑区、把最终内容落进对话流、记录光标落点。"""
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
        # 光标现在所在的绝对行（提交行的正下方，钳到滚动区底部）。
        # 用于给下一个任务初始化流式光标，避免任务中途竞态查询终端。
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
        """画输入行 + 补全菜单（绝对定位，避免把对话滚上去）；返回占用行数。"""
        rows, cols = style.term_size()
        rows, cols = max(5, rows), max(1, cols)
        bottom = max(1, rows - self._footer_rows())
        # 最后一列会折行；在 DECSTBM 底行折行会滚出第二个菜单。
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
            visual = visual[skip:]      # 菜单空间不够时，从顶部裁掉多余正文行
            cur_row = max(0, cur_row - skip)
            body_rows = len(visual)
        # prompt 下面需要空间：滚动 DECSTBM 区而不是把 prompt 盖到对话上。
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
        clear_to = bottom if wipe else min(bottom, prompt_row + max(old_rows, extra))   # wipe=全部擦；否则只擦旧菜单占的行
        indent = " " * prompt_w
        for r in range(clear_from, clear_to + 1):
            sys.stdout.write(f"\033[{r};1H\033[2K")
        sys.stdout.write(f"\033[{prompt_row};1H" + prompt + visual[0])
        for i, line in enumerate(visual[1:], start=1):
            sys.stdout.write(f"\033[{prompt_row + i};1H\033[2K" + indent + line)
        menu_row = prompt_row + body_rows
        for i, line in enumerate(menu):
            painted = style.cyan(line) if line.lstrip().startswith(">") and style.enabled() else style.dim(line)   # 选中行高亮
            sys.stdout.write(f"\033[{menu_row + i};1H\033[2K" + painted)
        cup_row = min(bottom, prompt_row + cur_row)
        cup_col = min(cols, max(1, cur_col + 1))
        sys.stdout.write(f"\033[{cup_row};{cup_col}H")   # 把光标放回编辑位置
        if self.on_paint:
            self.on_paint()
        sys.stdout.flush()
        self._prompt_row = prompt_row
        return extra

    def _save(self) -> None:
        """退出时把 readline 历史写回文件。"""
        if not self.available:
            return
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            self.readline.write_history_file(self.history_path)
        except OSError:
            pass


def pick_list(options: list[str], selected: int = 0) -> int | None:
    """方向键选择器。返回选中索引，取消（Esc/q/Ctrl+C/关闭）返回 None。"""
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
        """重画整个选择器（原地覆盖）。"""
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
                # 选择过程中 tty/管道关闭（窗口关闭、管道断开）：
                # 取消而不是 traceback——dispatch 只捕 KeyboardInterrupt。
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
            pass  # fd 已没了；上面的取消才是有意义的结果
        style.writeln("")


def completion_words(
    providers: Iterable[str],
    skills: Iterable[str] | None = None,
    effort_levels: Iterable[str] | None = None,
) -> list[str]:
    """Tab 补全词表：斜杠命令 + provider/effort 变体 + skill 名。"""
    words = list(COMMANDS)
    for name in providers:
        words.append(f"/provider {name}")
    for level in effort_levels or ():
        words.append(f"/effort {level}")
        words.append(f"/think {level}")
    words.extend(["/refine auto", "/refine auto off", "/jobs", "/jobs kill"])
    for skill in skills or ():
        words.append(f"/skill:{skill}")
    return words
