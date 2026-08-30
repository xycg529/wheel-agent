"""One shared mutable state object for the TUI process.

The app used to keep this as module-level globals (FOOTER, LIVE, ACTIVE,
SNIPS, AUTO_REFINE_EVERY, _refine_*). Splitting the monolith into
live/commands/refine modules needs one object every module can import,
instead of `global` declarations that only work inside a single module.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from wheel_agent.ui.style import Footer

if TYPE_CHECKING:
    from wheel_agent.ui.app import LiveTurn, ToolSnips


class AppState:
    def __init__(self) -> None:
        self.footer = Footer()
        # The LiveTurn currently streaming (None when idle); set per turn.
        self.live: "LiveTurn | None" = None
        # Handles for the foreground run: thread, turn queue, tool runtime,
        # model client, session. Read by the busy prompt and the /commands.
        self.active: dict[str, Any] = {
            "thread": None,
            "queue": None,
            "runtime": None,
            "model": None,
            "session": None,
        }
        # Snipped (clipped) tool outputs awaiting /expand.
        self.snips: "ToolSnips | None" = None
        # /refine auto: cadence in user turns, per-session due counters,
        # the pending batch, and the worker thread.
        self.auto_refine_every: int = 8
        self.refine_at: dict[str, int] = {}
        self.refine_lock = threading.Lock()
        self.refine_pending: list[dict] = []
        self.refine_thread: threading.Thread | None = None


STATE = AppState()
