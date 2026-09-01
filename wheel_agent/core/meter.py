"""计量表：token 数缩写、模型价格表、缓存命中/成本计算、页脚计量文本。"""

from __future__ import annotations

from wheel_agent.core.types import Usage


def compact_count(n: int) -> str:
    """把 token 数缩写成 12.3k / 1.2M 样式（页脚空间有限，只留一位小数）。"""
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
    """按模型名查（上下文窗口，输入/输出/缓存读/缓存写 美元价/每百万 token）。

    .env 里的 <前缀>_* 变量可覆盖查表结果；未知模型返回零价（不计费）。"""
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
    """这批用量里缓存命中的百分比，恒在 0–100。

    OpenAI 风格：cached ⊆ input_tokens（input 含缓存部分），命中 = cached/input。
    一些代理把 input 报成未命中部分（cached > input），此时命中 = cached/(cached+input)。"""
    cached = max(usage.cached_tokens, 0)
    inp = max(usage.input_tokens, 0)
    if cached <= 0:
        return 0.0   # 没有缓存命中
    if inp <= 0:
        return 100.0  # cached > 0 保证非空，input 为 0 时全部来自缓存
    if cached <= inp:
        return cached / inp * 100.0
    return cached / (cached + inp) * 100.0


def cost_usd(usage: Usage, input_price: float, output_price: float, cache_read_price: float, cache_write_price: float) -> float:
    """按四档价目算这批用量的美元成本；缓存命中部分按 cache_read 价计。"""
    cached = max(usage.cached_tokens, 0)
    inp = max(usage.input_tokens, 0)
    # OpenAI 计费口径：input 含缓存命中时，命中部分只收 cache_read 价。
    billed_in = inp - cached if cached <= inp else inp
    return (
        billed_in / 1_000_000 * input_price
        + cached / 1_000_000 * cache_read_price
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
    """页脚计量表一行：↑输入 ↓输出 R缓存 命中率 成本 上下文占用 汇总。"""
    # 优先展示最近一次调用的量（更贴近当前状态）；首次调用前退回总量。
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
