"""上下文紧凑（compaction）：历史超窗口时，把旧前缀压成一条摘要消息，
保留最近若干 token 原样。摘要作为新前缀，同时开启新的缓存纪元。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wheel_agent.core.context import estimate_item_tokens, estimate_items_tokens, tag_lines
from wheel_agent.core.model import ModelClient, extract_text, item_text
from wheel_agent.core.types import Item, Usage, unique

# 紧凑触发前留给输出的 token 余量。
RESERVE_TOKENS = 16_384
# 紧凑时保留最近多少 token 的原文不动。
KEEP_RECENT_TOKENS = 20_000
# 注入的摘要消息的开头标记（它伪装成 user 消息，但带这个标记以示区别）。
SUMMARY_MARK = "[SESSION SUMMARY — not a user message]"


@dataclass
class CompactStats:
    """一次紧凑的统计（事件流/UI 展示用）：是否做了、前后多少条/多少 token。"""

    did: bool = False
    before_items: int = 0
    after_items: int = 0
    before_tokens: int = 0
    after_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "did": self.did,
            "before_items": self.before_items,
            "after_items": self.after_items,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
        }

SUMMARIZE_INSTRUCTIONS = (
    # 给摘要模型的 system 指令：只回摘要正文。
    "You compress a coding-agent conversation into a structured summary. "
    "Return only the summary text, no preamble."
)

# 摘要的结构由 SUMMARIZE_PROMPT 硬约定：下次紧凑时 previous_summary 会把旧摘要喂回来，
# 新摘要按同样的标题更新，保证多轮紧凑不丢结构。
SUMMARIZE_PROMPT = """Summarize the following coding-agent history for a future turn.
Preserve decisions, constraints, file paths, and unfinished work.
Use exactly these headings:

## Goal
## Constraints & Preferences
## Progress
### Done
### In Progress
### Blocked
## Key Decisions
## Next Steps
## Critical Context
"""


def should_compact(input_tokens: int, context_window: int, reserve: int = RESERVE_TOKENS) -> bool:
    """触发判定：输入 token 超出（窗口 - 预留）就该压了。"""
    if context_window <= 0:
        return False
    return input_tokens > context_window - reserve


def is_context_overflow(exc: BaseException) -> bool:
    """从异常文本认出“上下文超窗”（各家 provider 措辞不同，列了常见说法）。"""
    text = str(exc).lower()
    needles = (
        "context_length_exceeded",
        "context length",
        "maximum context",
        "prompt is too long",
        "too many tokens",
        "token limit",
        "context window",
    )
    return any(needle in text for needle in needles)


def is_summary_item(item: Item) -> bool:
    """判断某条消息是不是之前注入的摘要（多轮紧凑时识别用）。"""
    if item.get("role") != "user":
        return False
    return SUMMARY_MARK in _item_text(item)


def _starts_valid_suffix(item: Item) -> bool:
    """该条能否作为保留后缀的开头（API 合法边界）。

    后缀不能从 tool_call/tool_output 对的中间开始，API 会拒。
    user/assistant 消息和 function_call 是安全起点。"""
    return item.get("role") in {"user", "assistant"} or item.get("type") == "function_call"


def find_user_cut_index(items: list[Item], keep_recent_tokens: int = KEEP_RECENT_TOKENS) -> int | None:
    """从尾部向前累计到 keep_recent_tokens，再对齐到下一个 user 边界作为切割点。

    只在 user 轮边界切，保证保留后缀是 API 合法的完整序列，
    摘要的契约也是干净的“该 user 轮之前的全部”。"""
    user_indices = [i for i, item in enumerate(items) if item.get("role") == "user"]
    if not user_indices:
        return None
    total = estimate_items_tokens(items)
    if total <= keep_recent_tokens:
        return None
    accumulated = 0
    threshold = 0
    reached = False
    for i in range(len(items) - 1, -1, -1):
        accumulated += estimate_item_tokens(items[i])
        if accumulated >= keep_recent_tokens:
            threshold = i
            reached = True
            break
    if not reached:
        return None
    for idx in user_indices:
        if idx >= threshold and idx > 0:
            return idx
    cut = user_indices[-1]
    if cut > 0:
        return cut
    # 长单任务特例：唯一 user 消息就是任务本身且在阈值之前，
    # 没有 user 边界可切。退而用第一个 API 合法边界，
    # 让运行还能压下去，而不是一路涨到溢出。
    for i in range(max(1, threshold), len(items)):
        if _starts_valid_suffix(items[i]):
            return i
    return None


def previous_summary(items: list[Item]) -> str:
    """找历史里已有的摘要文本（多轮紧凑时作为“待更新摘要”喂给模型）。"""
    for item in items:
        if is_summary_item(item):
            return _item_text(item)
    return ""


def collect_file_ops(items: list[Item]) -> tuple[list[str], list[str]]:
    """收集历史里读过的/改过的文件路径（含旧摘要里记录的），
    紧凑后以 <read-files>/<modified-files> 标签附在摘要尾部。"""
    read: list[str] = []
    modified: list[str] = []
    prior = previous_summary(items)
    if prior:
        read.extend(tag_lines(prior, "read-files"))
        modified.extend(tag_lines(prior, "modified-files"))
    for item in items:
        if item.get("type") != "function_call":
            continue
        args = item.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            continue
        path = args.get("path")
        if not path:
            continue
        name = str(item.get("name") or "")
        if name == "read":
            read.append(str(path))
        elif name in {"write", "edit"}:
            modified.append(str(path))
    return unique(read), unique(modified)


def compact_items(
    items: list[Item],
    model: ModelClient,
    workspace: str | Path,
    *,
    keep_recent_tokens: int = KEEP_RECENT_TOKENS,
    context_window: int = 0,
) -> tuple[list[Item], Usage]:
    """执行一次紧凑：返回新历史（摘要 + 保留后缀）和摘要调用的用量。

    找不到可切点时返回原列表对象（调用方用 is 判断 no-op）；
    保留部分仍超窗口时逐倍缩小 keep_recent_tokens 重试。"""
    usage = Usage()
    cut = find_user_cut_index(items, keep_recent_tokens)
    if cut is None:
        return items, usage
    if context_window > 0:
        kept_est = estimate_items_tokens(items[cut:])
        shrinking = keep_recent_tokens
        while kept_est > context_window - RESERVE_TOKENS and shrinking > 2_000:
            shrinking = shrinking // 2
            nxt = find_user_cut_index(items, shrinking)
            if nxt is None or nxt <= cut:
                break
            cut = nxt
            kept_est = estimate_items_tokens(items[cut:])
    prefix, kept = items[:cut], items[cut:]
    if not prefix:
        return items, usage   # 前缀为空：没东西可压
    summary, usage = _summarize(prefix, model, items)
    summary_item: Item = {"role": "user", "content": f"{SUMMARY_MARK}\n\n{summary}"}
    return [summary_item, *kept], usage


def compact_history(
    items: list[Item],
    model: ModelClient,
    workspace: str | Path,
    *,
    input_tokens: int = 0,
    context_window: int = 128_000,
    force: bool = False,
    plan_text: str = "",
) -> tuple[list[Item], Usage, CompactStats]:
    """循环里的紧凑入口：按需触发（force 或超窗）并回统计。

    不改写已发出的消息（会破坏 prompt 缓存前缀）；
    完整紧凑会用新摘要替换前缀，由调用方开启新缓存纪元。"""
    usage = Usage()
    # plan_text 是兼容参数：计划状态现在存 Session.plan，不在历史里。
    del plan_text
    before_tokens = estimate_items_tokens(items)
    stats = CompactStats(
        before_items=len(items),
        after_items=len(items),
        before_tokens=before_tokens,
        after_tokens=before_tokens,
    )
    compacted = items
    if force or should_compact(input_tokens, context_window):
        compacted, extra = compact_items(compacted, model, workspace, context_window=context_window)
        usage.add(extra)
        # 没东西可切时 compact_items 返回的是同一个列表对象，
        # 所以用 is 而不是长度来判 no-op。
        stats.did = compacted is not items
        stats.after_items = len(compacted)
        stats.after_tokens = estimate_items_tokens(compacted) if stats.did else before_tokens
    return compacted, usage, stats


def serialize_items(items: list[Item]) -> str:
    """把历史序列化成摘要模型能读的文本；工具输出超 4000 字符截断。"""
    lines: list[str] = []
    for item in items:
        kind = item.get("type") or item.get("role") or "item"
        if item.get("role") == "user":
            lines.append(f"[User]: {_item_text(item)}")
        elif kind == "function_call":
            lines.append(f"[Assistant tool call]: {item.get('name')} {item.get('arguments')}")
        elif kind == "function_call_output":
            output = str(item.get("output") or "")
            if len(output) > 4000:
                output = output[:4000] + "…"
            lines.append(f"[Tool result]: {output}")
        elif kind in {"message", "assistant"} or item.get("role") == "assistant":
            text = _item_text(item)
            if text:
                lines.append(f"[Assistant]: {text}")
        else:
            lines.append(f"[{kind}]: {json.dumps(item, ensure_ascii=False)[:2000]}")
    return "\n".join(lines)


def _summarize(prefix: list[Item], model: ModelClient, all_items: list[Item]) -> tuple[str, Usage]:
    """调模型把前缀压成摘要；失败时退回截断的原文。"""
    prior = previous_summary(prefix)
    read_files, modified_files = collect_file_ops(all_items)
    prompt = SUMMARIZE_PROMPT
    if prior:
        prompt += "\nPrevious summary to update:\n" + prior + "\n"   # 多轮紧凑：在旧摘要基础上更新
    prompt += "\nHistory:\n" + serialize_items(prefix)
    response = model.complete(
        [{"role": "user", "content": prompt}],
        tools=[],
        instructions=SUMMARIZE_INSTRUCTIONS,
    )
    text = extract_text(response.output).strip() or serialize_items(prefix)[:2000]
    # 文件清单靠模型可能漏，这里强制补齐。
    text = _ensure_file_tags(text, read_files, modified_files)
    return text, response.usage


def _ensure_file_tags(summary: str, read_files: list[str], modified_files: list[str]) -> str:
    """把读/改文件清单补成摘要尾部的标签块（模型漏写时）。"""
    body = summary.rstrip()
    if read_files and "<read-files>" not in body:
        body += "\n\n<read-files>\n" + "\n".join(read_files) + "\n</read-files>"
    if modified_files and "<modified-files>" not in body:
        body += "\n\n<modified-files>\n" + "\n".join(modified_files) + "\n</modified-files>"
    return body


def _item_text(item: Item) -> str:
    """取消息文本；function_call_output 的正文在 output 字段，补上这个回退。"""
    text = item_text(item)
    if not text and item.get("output"):
        return str(item["output"])
    return text
