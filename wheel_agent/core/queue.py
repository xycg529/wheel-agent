from __future__ import annotations

from collections import deque
from threading import Event, Lock


class AskWaiter:
    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        self.answer = False
        self._done = Event()

    def resolve(self, yes: bool) -> None:
        self.answer = bool(yes)
        self._done.set()

    def wait(self, abort: Event | None = None) -> bool:
        while not self._done.wait(0.1):
            if abort is not None and abort.is_set():
                self.answer = False
                return False
        return self.answer


class TurnQueue:
    """Steer = next LLM call; follow = after the run would otherwise stop; abort = stop after the current call."""

    def __init__(self) -> None:
        self._steer: deque[str] = deque()
        self._follow: deque[str] = deque()
        self._lock = Lock()
        self.abort = Event()
        self._ask: AskWaiter | None = None

    def steer(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        with self._lock:
            self._steer.append(text)

    def follow(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        with self._lock:
            self._follow.append(text)

    def drain_steer(self) -> list[str]:
        with self._lock:
            out = list(self._steer)
            self._steer.clear()
        return out

    def drain_follow(self) -> list[str]:
        with self._lock:
            out = list(self._follow)
            self._follow.clear()
        return out

    def request_ask(self, prompt: str) -> bool:
        waiter = AskWaiter(prompt)
        with self._lock:
            self._ask = waiter
        try:
            return waiter.wait(self.abort)
        finally:
            with self._lock:
                if self._ask is waiter:
                    self._ask = None

    def pending_ask(self) -> AskWaiter | None:
        with self._lock:
            return self._ask

