"""跨模块共享的类型定义：消息/工具/用量/安全裁决等 dataclass，
以及被 compact 和 safety 共用的保序去重 helper。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


# 对话历史里的一条消息/事件，用 dict 而非 dataclass：
# 要与 OpenAI 的 item 结构互转，宽松的 dict 方便按需增删字段。
Item = dict[str, Any]
# 安全裁决的三种结论：直接放行 / 弹窗询问用户 / 拒绝。
Decision = Literal["allow", "ask", "deny"]


@dataclass
class Usage:
    """一次模型调用的 token 计量；cached/cache_write 记录前缀缓存命中与写入。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: "Usage") -> None:
        """把另一次用量累加进来（整个 run 的总量 = 逐次调用求和）。"""
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_tokens += other.cached_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.reasoning_tokens += other.reasoning_tokens

    def as_dict(self) -> dict[str, int]:
        # 直接用 asdict：字段增减时序列化自动跟上，不会和 dataclass 脱节。
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Usage":
        """从会话 meta/事件 JSON 恢复用量；缺字段按 0 处理。"""
        data = data or {}
        return cls(
            input_tokens=int(data.get("input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
            cached_tokens=int(data.get("cached_tokens") or 0),
            cache_write_tokens=int(data.get("cache_write_tokens") or 0),
            reasoning_tokens=int(data.get("reasoning_tokens") or 0),
        )


class APIError(RuntimeError):
    """Provider 调用在重试后仍失败。带 transient/status，供循环决定是否报错收场。"""

    def __init__(self, message: str, *, transient: bool = False, status: int | None = None):
        super().__init__(message)
        # transient=True 表示是 4xx/5xx 临时故障：会话保留已完成回合，用户可直接重发。
        self.transient = transient
        self.status = status


@dataclass
class ModelResponse:
    """客户端对外的统一响应：把 Responses/Chat 两种协议归一成同一份 item 列表。"""

    output: list[Item]
    usage: Usage = field(default_factory=Usage)
    raw_id: str = ""


@dataclass
class FunctionCall:
    """模型要求的工具调用；raw_arguments 保留原始 JSON 串供审计重放。"""

    call_id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass
class ToolResult:
    """工具执行结果；blocked/safety_* 字段原样带回安全裁决，进事件流供 UI 标红。"""

    call_id: str
    name: str
    output: str
    is_error: bool = False
    blocked: bool = False
    safety_decision: str = ""
    safety_reason: str = ""
    safety_source: str = ""


@dataclass
class SafetyVerdict:
    """安全门对一次调用的裁决：结论 + 理由 + 来源（规则/记忆/用户）。"""

    decision: Decision
    reason: str
    source: str = "rules"


@dataclass
class RunResult:
    """一次 run_agent 的收尾产物：CLI --json、REPL 计量表、replay 分类都从这里取数。"""

    run_id: str
    text: str
    turns: int
    usage: Usage
    tool_results: list[ToolResult]
    stop_reason: str
    events_path: str = ""
    items: list[Item] = field(default_factory=list)
    last_usage: Usage = field(default_factory=Usage)
    changed_files: list[str] = field(default_factory=list)
    task_id: str = ""
    replay_status: str = ""
    replay_details: dict[str, Any] = field(default_factory=dict)


def unique(values: list[str]) -> list[str]:
    """保序去重：compact 的文件清单、safety 的路径列表都靠它保持首次出现顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
