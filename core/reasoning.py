"""统一的推理档位刻度：各 provider 的档位名单归一到同一套 LEVELS，
并提供钳制（不支持时向上/向下找最近可用档）与按 API 组装。"""

from __future__ import annotations

from typing import Iterable

# 统一刻度跨 provider 复用。OpenAI Responses 把 off 拼作 none；
# max 是 GLM/Zhipu 的最高档，与 xhigh 分开，避免 .env 名单被改写成 xhigh。
LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
# 常见写法别名，normalize 时归一。
ALIASES = {"none": "off", "x-high": "xhigh", "extra-high": "xhigh"}
# 统一刻度 → API 字段值的映射（off 在 Responses API 里是 none）。
API_EFFORT = {
    "off": "none",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


def normalize(level: str) -> str:
    """把任意写法归一到统一刻度（大小写、别名）。"""
    key = level.strip().lower()
    return ALIASES.get(key, key)


def parse_levels(raw: str) -> tuple[str, ...]:
    """解析 .env 里逗号分隔的档位名单；未知档位直接报错，配置错误越早暴露越好。"""
    if not raw.strip():
        return ()
    out: list[str] = []
    for part in raw.split(","):
        level = normalize(part)
        if not level:
            continue
        if level not in LEVELS:
            raise ValueError(f"unknown reasoning level {part!r}; use {', '.join(LEVELS)}")
        if level not in out:
            out.append(level)
    return tuple(out)


def infer_effort_levels(model: str) -> tuple[str, ...]:
    """按模型名猜可用档位；空元组 = 非推理模型，完全不发 reasoning 参数。"""
    name = model.lower()
    if any(tag in name for tag in ("gpt-4o", "gpt-4.1", "gpt-4-turbo", "gpt-3.5", "deepseek-chat", "glm-4-flash")):
        return ()
    if any(tag in name for tag in ("o1-mini", "o1-preview")):
        return ("low", "medium", "high")
    if any(tag in name for tag in ("o1", "o3", "o4")):
        return ("low", "medium", "high")
    if "gpt-5" in name or "codex" in name:
        return ("off", "minimal", "low", "medium", "high", "xhigh")
    if "grok" in name:
        return ("low", "medium", "high", "xhigh")
    if any(tag in name for tag in ("reason", "think", "r1")):
        return ("low", "medium", "high")
    return ()


def clamp_effort(requested: str, supported: Iterable[str]) -> str | None:
    """请求的档位不支持时先向上找、再向下找；返回 None = 省略该参数。"""
    allowed = tuple(normalize(item) for item in supported)
    allowed = tuple(item for item in allowed if item in LEVELS)   # 先剔除名单外的项
    if not allowed:
        return None
    want = normalize(requested) if requested else "medium"
    if want not in LEVELS:
        want = "medium"
    if want in allowed:
        return want
    try:
        index = LEVELS.index(want)
    except ValueError:
        index = LEVELS.index("medium")
    for candidate in LEVELS[index + 1 :]:
        if candidate in allowed:
            return candidate
    for candidate in reversed(LEVELS[:index]):
        if candidate in allowed:
            return candidate
    return None


def reasoning_payload(requested: str, supported: Iterable[str]) -> dict[str, str] | None:
    """组装发往 API 的 reasoning 字段；None = 该模型/档位不发送。"""
    clamped = clamp_effort(requested, supported)
    if clamped is None or clamped == "off":   # 非推理模型或显式 off：不发，兼容所有端点
        return None
    return {"effort": API_EFFORT[clamped], "summary": "detailed"}
