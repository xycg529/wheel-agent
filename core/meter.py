from __future__ import annotations

from wheel_agent.core.types import Usage


def compact_count(n: int) -> str:
    value = float(n)
    if abs(value) >= 1_000_000:
        text = f"{value / 1_000_000:.1f}M"
        return text.replace(".0M", "M")
    if abs(value) >= 10_000:
        return f"{value / 1000:.0f}k"
    if abs(value) >= 1000:
        text = f"{value / 1000:.1f}k"
        return text.replace(".0k", "k")
    return str(int(n))


def infer_model_profile(model: str) -> tuple[int, float, float, float, float]:
    """context_window, input/output/cache_read/cache_write USD per 1M tokens."""
    name = model.lower()
    if "gpt-5.6" in name:
        return 272_000, 5.0, 30.0, 0.5, 6.25
    if "gpt-5" in name or "codex" in name:
        return 256_000, 1.25, 10.0, 0.125, 0.0
    if "grok-4" in name:
        return 500_000, 2.0, 6.0, 0.3, 0.0
    if "gpt-4.1" in name:
        return 1_047_576, 2.0, 8.0, 0.5, 0.0
    if "gpt-4o" in name:
        return 128_000, 2.5, 10.0, 1.25, 0.0
    if "deepseek" in name:
        return 128_000, 0.28, 0.42, 0.028, 0.0
    return 128_000, 0.0, 0.0, 0.0, 0.0


def cache_hit_pct(usage: Usage) -> float:
    """Percent of this usage blob that was served from cache.

    OpenAI-style: cached ⊆ input_tokens.
    Some proxies report input as uncached only (cached > input); then
    hit = cached / (cached + input). Always clamped to 0–100.
    """
    cached = max(usage.cached_tokens, 0)
    inp = max(usage.input_tokens, 0)
    if cached <= 0:
        return 0.0
    if inp <= 0:
        return 100.0 if cached else 0.0
    if cached <= inp:
        return cached / inp * 100.0
    return cached / (cached + inp) * 100.0


def cost_usd(usage: Usage, input_price: float, output_price: float, cache_read_price: float, cache_write_price: float) -> float:
    cached = max(usage.cached_tokens, 0)
    inp = max(usage.input_tokens, 0)
    if cached <= inp:
        billed_in = inp - cached
        cache_reads = cached
    else:
        billed_in = inp
        cache_reads = cached
    return (
        billed_in / 1_000_000 * input_price
        + cache_reads / 1_000_000 * cache_read_price
        + usage.output_tokens / 1_000_000 * output_price
        + usage.cache_write_tokens / 1_000_000 * cache_write_price
    )


def format_meter(
    total: Usage,
    last: Usage,
    *,
    context_window: int,
    input_price: float,
    output_price: float,
    cache_read_price: float,
    cache_write_price: float,
    compact_runs: int = 0,
) -> str:
    req = last if (last.input_tokens or last.output_tokens or last.cached_tokens) else total
    hit = cache_hit_pct(req)
    dollars = cost_usd(total, input_price, output_price, cache_read_price, cache_write_price)
    occupied = req.input_tokens if req.input_tokens else total.input_tokens
    parts = [
        f"↑{compact_count(req.input_tokens)}",
        f"↓{compact_count(req.output_tokens)}",
        f"R{compact_count(req.cached_tokens)}",
        f"CH{hit:.1f}%",
        f"${dollars:.3f}",
    ]
    if context_window > 0:
        pct = occupied / context_window * 100.0
        parts.append(f"{pct:.1f}%/{compact_count(context_window)}")
    if total.input_tokens and total.input_tokens != req.input_tokens:
        parts.append(f"Σ↑{compact_count(total.input_tokens)}")
    if compact_runs:
        parts.append(f"C{compact_runs}")
    return " ".join(parts)
