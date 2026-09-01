"""运行中的三条输入通道：steer（并入下一轮）、follow（本轮结束后投递）、
abort（停机信号）和 y/N 询问的跨线程交接。线程安全。"""

from __future__ import annotations

from collections import deque
from threading import Event, Lock


class AskWaiter:
    """一次 y/N 询问的跨线程交接：工作线程提问后等主线程 resolve。"""

    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        self.answer = False
        self._done = Event()

    def resolve(self, yes: bool) -> None:
        """主线程拿到答案后回填并唤醒等待方。"""
        self.answer = bool(yes)
        self._done.set()

    def wait(self, abort: Event | None = None) -> bool:
        """等待答案；abort 置位时不再等（默认拒绝），避免工具线程永久挂起。"""
        while not self._done.wait(0.1):   # 分段等待：每 0.1s 检查一次 abort，保持可中断
            if abort is not None and abort.is_set():
                self.answer = False
                return False
        return self.answer


class TurnQueue:
    """steer = 并入下一轮模型调用；follow = 本轮正常结束后作为新回合；abort = 当前调用后停机。"""

    def __init__(self) -> None:
        self._steer: deque[str] = deque()
        self._follow: deque[str] = deque()
        self._lock = Lock()
        self.abort = Event()
        self._ask: AskWaiter | None = None   # 当前悬而未决的 y/N 询问，主线程轮询消费

    def _push(self, q: deque[str], text: str) -> None:
        # 空文本不排队：回车产生的空 steer 没有意义。
        text = text.strip()
        if text:
            with self._lock:
                q.append(text)

    def _drain(self, q: deque[str]) -> list[str]:
        # 一次取空：循环只在两次模型调用之间看队列，避免半途打断。
        with self._lock:
            out = list(q)
            q.clear()
        return out

    def steer(self, text: str) -> None:
        self._push(self._steer, text)

    def follow(self, text: str) -> None:
        self._push(self._follow, text)

    def drain_steer(self) -> list[str]:
        return self._drain(self._steer)

    def drain_follow(self) -> list[str]:
        return self._drain(self._follow)

    def request_ask(self, prompt: str) -> bool:
        """工作线程发起 y/N：挂上 waiter 阻塞等待，主线程经 pending_ask() 消费。"""
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
        """主线程轮询：有未决询问就回到主线程弹窗，避免嵌套 readline 抢输入。"""
        with self._lock:
            return self._ask

