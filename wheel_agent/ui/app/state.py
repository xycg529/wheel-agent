"""TUI 进程共享的可变状态对象。

应用以前把这些放在模块级全局（FOOTER、LIVE、ACTIVE、SNIPS、AUTO_REFINE_EVERY、
_refine_*）。拆成 live/commands/refine 模块后需要一个所有模块都能导入的对象，
而不是只在单个模块内有效的 global 声明。"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from wheel_agent.ui.style import Footer

if TYPE_CHECKING:
    from wheel_agent.ui.app import LiveTurn, ToolSnips


class AppState:
    """单例 AppState 的各字段。"""

    def __init__(self) -> None:
        self.footer = Footer()
        # 当前正在流式输出的 LiveTurn（空闲时 None）；每回合设置。
        self.live: "LiveTurn | None" = None
        # 前台运行的句柄：线程、回合队列、工具运行时、模型客户端、会话。
        # busy 提示和 /命令 读这里。
        self.active: dict[str, Any] = {
            "thread": None,
            "queue": None,
            "runtime": None,
            "model": None,
            "session": None,
        }
        # 被截断（裁剪）的、等 /expand 展开的工具输出。
        self.snips: "ToolSnips | None" = None
        # /refine 自动：每几个用户轮触发一次、每会话的到期计数、
        # 待处理的批次和工作线程。
        self.auto_refine_every: int = 8
        self.refine_at: dict[str, int] = {}
        self.refine_lock = threading.Lock()
        self.refine_pending: list[dict] = []
        self.refine_thread: threading.Thread | None = None


# 全局单例。
STATE = AppState()
