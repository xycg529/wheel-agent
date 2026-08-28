from __future__ import annotations

from typing import Iterable

# Unified scale from Pi ch.4. OpenAI Responses uses "none" instead of "off".
# `max` is GLM/Zhipu's top tier; keep it distinct so .env lists are not rewritten to xhigh.
LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
ALIASES = {"none": "off", "x-high": "xhigh", "extra-high": "xhigh"}
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
    key = level.strip().lower()
    return ALIASES.get(key, key)


def parse_levels(raw: str) -> tuple[str, ...]:
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
    """Guess which unified levels a model accepts. Empty = do not send reasoning."""
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
    """Pi clamp: if missing, search upward first, then downward. None = omit param."""
    allowed = tuple(normalize(item) for item in supported)
    allowed = tuple(item for item in allowed if item in LEVELS)
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
    clamped = clamp_effort(requested, supported)
    if clamped is None or clamped == "off":
        return None
    return {"effort": API_EFFORT[clamped], "summary": "detailed"}
