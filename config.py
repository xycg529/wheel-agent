from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

from wheel_agent.meter import infer_model_profile
from wheel_agent.reasoning import infer_effort_levels, normalize, parse_levels


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key: str
    base_url: str
    model: str
    effort_levels: tuple[str, ...] = ()
    context_window: int = 128_000
    input_price: float = 0.0
    output_price: float = 0.0
    cache_read_price: float = 0.0
    cache_write_price: float = 0.0
    api: str = "responses"  # responses | chat


@dataclass
class AgentConfig:
    provider: ProviderConfig
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    max_turns: int = 0
    runs_dir: Path = field(default_factory=lambda: Path(".wheel_runs"))
    interactive: bool = True
    effort: str = "medium"

    def with_provider(self, name: str) -> "AgentConfig":
        key = name.strip().lower()
        if key not in self.providers:
            known = ", ".join(sorted(self.providers)) or "(none)"
            raise KeyError(f"unknown provider {name!r}; configured: {known}")
        return replace(self, provider=self.providers[key])

    def with_effort(self, level: str) -> "AgentConfig":
        return replace(self, effort=normalize(level))

    def with_max_turns(self, n: int) -> "AgentConfig":
        return replace(self, max_turns=int(n))


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _discover_provider_names() -> list[str]:
    explicit = [p.strip().lower() for p in _env("PROVIDERS").split(",") if p.strip()]
    if explicit:
        return explicit
    names: list[str] = []
    for key, value in os.environ.items():
        if key.endswith("_API_KEY") and value.strip():
            names.append(key[: -len("_API_KEY")].lower())
        elif key.endswith("_BASE_URL") and value.strip():
            prefix = key[: -len("_BASE_URL")].lower()
            if prefix not in names:
                names.append(prefix)
    if "openai" not in names and (_env("OPENAI_API_KEY") or _env("OPENAI_BASE_URL")):
        names.insert(0, "openai")
    if not names:
        names = ["openai"]
    # Preserve order but drop duplicates.
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def normalize_api(raw: str, base_url: str = "") -> str:
    key = (raw or "").strip().lower().replace("-", "_")
    if key in {"chat", "chat_completions", "completions", "v1_chat_completions"}:
        return "chat"
    if key in {"responses", "response"}:
        return "responses"
    url = (base_url or "").rstrip("/").lower()
    if url.endswith("/chat/completions"):
        return "chat"
    return "responses"


def strip_endpoint_path(base_url: str) -> str:
    url = (base_url or "").rstrip("/")
    lowered = url.lower()
    for suffix in ("/chat/completions", "/completions", "/responses"):
        if lowered.endswith(suffix):
            return url[: -len(suffix)]
    return url


def _load_provider(name: str) -> ProviderConfig:
    prefix = name.upper()
    api_key = _env(f"{prefix}_API_KEY") or _env("OPENAI_API_KEY")
    raw_url = _env(f"{prefix}_BASE_URL") or "https://api.openai.com/v1"
    api_raw = _env(f"{prefix}_API") or _env(f"{prefix}_API_FORMAT") or _env("WHEEL_API") or _env("API_FORMAT")
    api = normalize_api(api_raw, raw_url)
    base_url = strip_endpoint_path(raw_url)
    model = _env(f"{prefix}_MODEL") or _env("MODEL") or "gpt-4.1-mini"
    levels_raw = _env(f"{prefix}_REASONING_LEVELS") or _env(f"{prefix}_EFFORT_LEVELS")
    levels = parse_levels(levels_raw) if levels_raw else infer_effort_levels(model)
    window, in_price, out_price, cache_read, cache_write = infer_model_profile(model)
    return ProviderConfig(
        name=name,
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        model=model,
        effort_levels=levels,
        context_window=int(_env(f"{prefix}_CONTEXT_WINDOW") or window),
        input_price=float(_env(f"{prefix}_INPUT_PRICE") or in_price),
        output_price=float(_env(f"{prefix}_OUTPUT_PRICE") or out_price),
        cache_read_price=float(_env(f"{prefix}_CACHE_READ_PRICE") or cache_read),
        cache_write_price=float(_env(f"{prefix}_CACHE_WRITE_PRICE") or cache_write),
        api=api,
    )


def load_config(env_file: str | Path | None = None, interactive: bool = True) -> AgentConfig:
    if env_file:
        load_dotenv(env_file, override=False)
    else:
        here = Path(__file__).resolve().parent.parent / ".env"
        load_dotenv(here, override=False)
        load_dotenv(override=False)

    names = _discover_provider_names()
    providers = {name: _load_provider(name) for name in names}
    default = _env("DEFAULT_PROVIDER", names[0]).lower()
    if default not in providers:
        providers[default] = _load_provider(default)

    runs_dir = Path(_env("WHEEL_RUNS_DIR") or ".wheel_runs")
    # REPL and --json stay unlimited. Eval suites read MAX_TURNS themselves.
    max_turns = 0
    effort = normalize(
        _env(f"{providers[default].name.upper()}_REASONING_EFFORT")
        or _env("REASONING_EFFORT")
        or _env("EFFORT")
        or "medium"
    )
    return AgentConfig(
        provider=providers[default],
        providers=providers,
        max_turns=max_turns,
        runs_dir=runs_dir,
        interactive=interactive,
        effort=effort,
    )
